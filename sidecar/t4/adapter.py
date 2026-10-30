from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import nullcontext
import json
from dataclasses import dataclass
import threading
import time
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

from sidecar.at.subscription import (
    PluginDescriptor,
    SubscriptionParseError,
    SubscriptionFetcher,
    SubscriptionSnapshot,
    resolve_subscription,
)
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.gateway.navigation_settings import NavigationSettingsStore, can_search
from sidecar.t4.opaque import OpaqueIdStore, OpaqueValue
from sidecar.diagnostics import record, trace_id, trace_scope
from sidecar.plugin_proxy import PluginProxyRegistry, rewrite_proxy_urls
from sidecar.playback import PlaybackBridge
from sidecar.images import ImageBridge
from sidecar.worker.browse import BrowseContextStore


class WorkerInvoker(Protocol):
    def call(
        self,
        runtime_key: str,
        plugin: PluginDescriptor,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        ...


class NativeInvoker(Protocol):
    def call(
        self,
        runtime_key: str,
        plugin: PluginDescriptor,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        ...


@dataclass(frozen=True, slots=True)
class T4Request:
    module: str
    source_id: str
    plugin_id: str | None
    params: dict[str, Any]
    proxy_base: str = ""


class T4Adapter:
    def __init__(
        self,
        source_registry: SourceRegistry,
        worker_invoker: WorkerInvoker,
        *,
        subscription_fetcher: SubscriptionFetcher | None = None,
        opaque_ids: OpaqueIdStore | None = None,
        native_invoker: NativeInvoker | None = None,
        subscription_resolver: Any | None = None,
        local_snapshot_provider: Any | None = None,
        playback_bridge: PlaybackBridge | None = None,
        image_bridge: ImageBridge | None = None,
        navigation_settings: NavigationSettingsStore | None = None,
    ) -> None:
        self._sources = source_registry
        self._worker = worker_invoker
        self._native = native_invoker
        self._fetcher = subscription_fetcher
        self._opaque_ids = opaque_ids or OpaqueIdStore()
        self._subscription_resolver = subscription_resolver
        self._local_snapshot_provider = local_snapshot_provider
        self._playback = playback_bridge
        self._images = image_bridge
        self._navigation_settings = navigation_settings
        self._plugin_locks: dict[str, asyncio.Lock] = {}
        self._navigation_cards: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        self._catalog_metadata: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        self._navigation_slots = asyncio.Semaphore(3)
        self._navigation_budget = 25.0
        self._navigation_tasks: set[asyncio.Task] = set()

    def plugin_lock(self, source_id: str, plugin_id: str) -> asyncio.Lock:
        key = f"{source_id}:{plugin_id}"
        return self._plugin_locks.setdefault(key, asyncio.Lock())

    def release_memory(self) -> int:
        return self._opaque_ids.release_memory()

    def close(self) -> None:
        for task in self._navigation_tasks:
            task.cancel()
        self._opaque_ids.close()
        self._navigation_cards.clear()
        self._catalog_metadata.clear()

    async def reset_plugin(self, source_id: str, plugin_id: str) -> None:
        for key in tuple(self._catalog_metadata):
            if key[:2] == (source_id, plugin_id):
                self._catalog_metadata.pop(key, None)
        reset = getattr(self._worker, "reset", None)
        if reset is not None:
            await asyncio.to_thread(reset, f"{source_id}:{plugin_id}")

    async def dispatch(self, request: T4Request) -> dict[str, Any]:
        with trace_scope(
            source=request.source_id, plugin=request.plugin_id or "default",
            module=request.module,
        ):
            start = time.monotonic()
            record("adapter.start")
            try:
                result = await self._dispatch(request)
                record("adapter.complete", count=len(result.get("list") or []),
                       duration_ms=round((time.monotonic() - start) * 1000))
                return result
            except Exception as exc:
                record("adapter.failed", error=type(exc).__name__,
                       duration_ms=round((time.monotonic() - start) * 1000))
                raise

    async def _snapshot(self, source: SourceConfig) -> SubscriptionSnapshot:
        snapshot = None
        if self._local_snapshot_provider is not None:
            snapshot = self._local_snapshot_provider(source.source_id)
        if snapshot is not None:
            return snapshot
        if self._subscription_resolver is not None:
            return await self._subscription_resolver(source)
        return await resolve_subscription(source, self._fetcher)

    async def _dispatch(self, request: T4Request) -> dict[str, Any]:
        source = self._sources.get(request.source_id)
        if source is None:
            raise SubscriptionParseError("source was not found")
        snapshot = await self._snapshot(source)
        plugin = snapshot.plugin_for(request.plugin_id)
        if plugin.api == "csp_PianDan" and request.module == "detail":
            return await self._navigation_detail(request, source, plugin, snapshot)
        if plugin.api == "csp_PianDan" and request.module == "play":
            raw_id = str(request.params.get("play") or request.params.get("id") or "")
            target = self._opaque_ids.get(
                raw_id, source_id=source.source_id,
                plugin_id=plugin.plugin_id, kind="navigation",
            )
            if target:
                plugin_id, flag, play_id = json.loads(target)
                record("navigation.play_delegate", parent_trace=self._opaque_ids.origin_trace(raw_id))
                return await self.dispatch(T4Request(
                    "play", source.source_id, plugin_id,
                    {**request.params, "play": play_id, "flag": flag},
                    request.proxy_base,
                ))
        if source.source_id == "local-uploaded":
            async with self.plugin_lock(source.source_id, plugin.plugin_id):
                # Management may have deleted or refreshed it while this call waited.
                current = self._local_snapshot_provider(source.source_id)
                plugin = current.plugin_for(plugin.plugin_id)
                return await self._invoke(request, source, plugin)
        return await self._invoke(request, source, plugin)

    async def _navigation_detail(
        self, request: T4Request, source: SourceConfig, plugin: PluginDescriptor,
        snapshot: SubscriptionSnapshot,
    ) -> dict[str, Any]:
        _, params = self._translate("detail", request.params)
        ids = params["ids"]
        raw_id = self.unwrap_id(
            str(ids[0]) if ids else "", source_id=source.source_id,
            plugin_id=plugin.plugin_id, kind="detail",
        )
        card = self._navigation_cards.get((source.source_id, plugin.plugin_id, raw_id))
        if card is None:
            raise SubscriptionParseError("片单卡片已过期，请刷新片单后重新选择")
        title = str(card.get("vod_name") or "").strip()
        if not title:
            raise SubscriptionParseError("片单卡片缺少可搜索的片名")

        def title_key(value: Any) -> str:
            return re.sub(r"[\W_]+", "", str(value)).casefold()

        selected = (self._navigation_settings.selection(source.source_id)
                    if self._navigation_settings is not None else None)
        candidates = [item for item in snapshot.plugins if can_search(item)
                      and (selected is None or item.plugin_id in selected)]
        if not candidates:
            raise SubscriptionParseError("片单搜源范围为空，请在 T4 管理台勾选可搜索插件")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._navigation_budget
        results: dict[int, Any] = {}
        queue = iter(enumerate(candidates))

        async def find():
            for index, target in queue:
                # A timed-out HTTP response must not free slots while its synchronous
                # backend is still running. Finish only the active call, never its queue.
                try:
                    async with asyncio.timeout_at(deadline):
                        await self._navigation_slots.acquire()
                except TimeoutError:
                    return
                try:
                    if loop.time() >= deadline:
                        return
                    search = await self.dispatch(T4Request(
                        "search", source.source_id, target.plugin_id,
                        {"wd": title, "quick": False, "pg": 1,
                         "client": request.params.get("client", "")},
                        request.proxy_base,
                    ))
                    match = next((
                        vod for vod in search.get("list", [])
                        if isinstance(vod, dict)
                        and title_key(vod.get("vod_name")) == title_key(title)
                        and not (
                            card.get("vod_year") and vod.get("vod_year")
                            and str(card["vod_year"]) != str(vod["vod_year"])
                        )
                    ), None)
                    if match is None or loop.time() >= deadline:
                        continue
                    detail = await self.dispatch(T4Request(
                        "detail", source.source_id, target.plugin_id,
                        {"ids": match["vod_id"], "client": request.params.get("client", "")},
                        request.proxy_base,
                    ))
                    if loop.time() < deadline:
                        results[index] = target, next(iter(detail.get("list") or []), {})
                except Exception as exc:
                    record("navigation.source_failed", plugin=target.plugin_id,
                           error=type(exc).__name__)
                finally:
                    self._navigation_slots.release()

        record("navigation.search", count=len(candidates))
        tasks = [asyncio.create_task(find()) for _ in range(min(3, len(candidates)))]
        for task in tasks:
            self._navigation_tasks.add(task)
            task.add_done_callback(self._navigation_tasks.discard)
        try:
            _, pending = await asyncio.wait(tasks, timeout=self._navigation_budget)
        finally:
            deadline = loop.time()
        if pending:
            record("navigation.budget_exhausted", count=len(results))
        flags, groups = [], []
        for index in sorted(results):
            target, vod = results[index]
            for flag, group in zip(
                str(vod.get("vod_play_from") or "").split("$$$"),
                str(vod.get("vod_play_url") or "").split("$$$"),
            ):
                entries = []
                for episode in group.split("#"):
                    label, _, play_id = episode.partition("$")
                    if not play_id:
                        continue
                    delegated = self._opaque_ids.put(OpaqueValue(
                        source.source_id, plugin.plugin_id, "navigation",
                        json.dumps([target.plugin_id, flag, play_id]), trace_id(),
                    ))
                    entries.append(label + "$" + delegated)
                if entries:
                    flags.append(target.name.replace("$", "") + " · " + flag)
                    groups.append("#".join(entries))
        record("navigation.results", count=len(groups))
        if not groups:
            raise SubscriptionParseError("已选搜源范围在本次时限内未找到同名可播资源，请调整范围或使用全局搜索")
        item = dict(card)
        item.update(
            vod_id=str(ids[0]), vod_play_from="$$$".join(flags),
            vod_play_url="$$$".join(groups),
        )
        if self._images is not None and isinstance(item.get("vod_pic"), str):
            item["vod_pic"] = self._images.prepare(
                item["vod_pic"], request.proxy_base, plugin.backend_api,
                rules=plugin.image_headers,
            )
        return {"list": [item]}

    async def _invoke(
        self, request: T4Request, source: SourceConfig, plugin: PluginDescriptor
    ) -> dict[str, Any]:
        method, params = self._translate(request.module, request.params)
        params = self._unwrap_params(
            request.module,
            params,
            source.source_id,
            plugin.plugin_id,
        )
        runtime_key = f"{request.source_id}:{plugin.plugin_id}"
        backend = (
            "official_t4" if plugin.t4_endpoint
            else "native" if plugin.plugin_type == "builtin"
            else "worker"
        )
        server_proxy = source.cloud_playback == "server"
        if server_proxy and (backend == "worker" or method == "play"):
            params["_server_proxy"] = True
        record("backend.invoke", backend=backend)
        if plugin.plugin_type == "builtin" or plugin.t4_endpoint:
            if self._native is None:
                raise SubscriptionParseError("native bridge is not configured")
            result = await asyncio.to_thread(
                self._native.call,
                runtime_key,
                plugin,
                method,
                params,
            )
        else:
            params["_proxy_base"] = request.proxy_base
            result = await asyncio.to_thread(
                self._worker.call,
                runtime_key,
                plugin,
                method,
                params,
            )
        payload = self._normalize(
            request.module,
            result,
            source,
            plugin,
        )
        if self._images is not None and request.module in {"home", "category", "search", "detail"}:
            for item in payload.get("list") or []:
                if isinstance(item, dict) and isinstance(item.get("vod_pic"), str):
                    item["vod_pic"] = self._images.prepare(
                        item["vod_pic"], request.proxy_base, plugin.backend_api,
                        rules=plugin.image_headers,
                    )
        if request.module == "play" and server_proxy and self._playback is not None:
            payload = self._playback.relay_at(
                payload, request.proxy_base, backend_api=plugin.backend_api,
                token=plugin.token, internal_url=source.at_internal_url,
            )
        return payload

    def _unwrap_params(
        self,
        module: str,
        params: dict[str, Any],
        source_id: str,
        plugin_id: str,
    ) -> dict[str, Any]:
        output = dict(params)
        if module == "category":
            output["tid"] = self.unwrap_id(
                str(output.get("tid") or ""), source_id=source_id,
                plugin_id=plugin_id, kind="detail",
            )
        elif module == "detail":
            output["ids"] = [
                self.unwrap_id(
                    str(value),
                    source_id=source_id,
                    plugin_id=plugin_id,
                    kind="detail",
                )
                for value in output.get("ids", [])
            ]
        elif module in {"play", "action"}:
            output["id"] = self.unwrap_id(
                str(output.get("id") or output.get("play") or ""),
                source_id=source_id,
                plugin_id=plugin_id,
                kind="play",
            )
        return output

    @staticmethod
    def _translate(module: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if module == "home":
            return "home", {
                "filter": _bool_param(params.get("filter"), True),
                "client": str(params.get("client") or ""),
            }
        if module == "category":
            return (
                "category",
                {
                    "tid": str(params.get("tid") or params.get("t") or ""),
                    "pg": str(params.get("pg") or "1"),
                    "filter": _bool_param(params.get("filter"), True),
                    "extend": _json_or_text(params.get("extend", "")),
                    "native_filter": _filter_map(params),
                    "sort": params.get("sort"),
                    "size": params.get("size"),
                    "depth": params.get("depth"),
                    "type": params.get("type"),
                    "ac": params.get("ac"),
                    "client": str(params.get("client") or ""),
                    "platform": params.get("platform"),
                    "year": params.get("year"),
                    "genre": params.get("genre"),
                    "region": params.get("region"),
                },
            )
        if module == "search":
            return (
                "search",
                {
                    "key": str(params.get("wd") or params.get("key") or ""),
                    "quick": _bool_param(params.get("quick"), True),
                    "pg": str(params.get("pg") or "1"),
                    "native_filter": _filter_map(params),
                    "client": str(params.get("client") or ""),
                    "size": params.get("size"),
                },
            )
        if module == "detail":
            raw_ids = params.get("ids") or params.get("id") or ""
            ids = (
                raw_ids
                if isinstance(raw_ids, list)
                else [value for value in str(raw_ids).split(",") if value]
            )
            return "detail", {
                "ids": ids,
                "ac": params.get("ac"),
                "depth": params.get("depth"),
                "title": params.get("title"),
                "platform": params.get("platform"),
                "client": str(params.get("client") or ""),
            }
        if module == "play":
            return (
                "play",
                {
                    "flag": str(params.get("flag") or ""),
                    "id": str(params.get("play") or params.get("id") or ""),
                    "vip_flags": _json_or_text(params.get("vipFlags", [])),
                    "client": str(params.get("client") or ""),
                    "dash": _bool_param(params.get("dash"), False),
                    "from": str(params.get("from") or ""),
                    "type": str(params.get("type") or ""),
                    "t": params.get("t"),
                },
            )
        if module == "action":
            return (
                "action",
                {
                    "action": str(params.get("action") or ""),
                    "id": str(params.get("id") or params.get("play") or ""),
                    "client": str(params.get("client") or ""),
                    "from": str(params.get("from") or ""),
                    "type": str(params.get("type") or ""),
                },
            )
        raise SubscriptionParseError(f"unsupported T4 module: {module}")

    def _normalize(
        self,
        module: str,
        result: Any,
        source: SourceConfig,
        plugin: PluginDescriptor,
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise SubscriptionParseError(f"{module} result must be an object")
        if module in {"home", "category", "search", "detail"}:
            warnings = result.get("t4_warnings")
            if isinstance(warnings, list) and warnings:
                record("cloud.detail_partial", count=len(warnings), reason="share_expansion_failed")
            payload = dict(result)
            raw_list = payload.get("list")
            if isinstance(raw_list, list):
                payload["list"] = [
                    self._normalize_vod(item, source, plugin, module=module)
                    if isinstance(item, dict)
                    else item
                    for item in raw_list
                ]
            else:
                payload["list"] = []
            return payload
        if module in {"play", "action"}:
            return self._normalize_play(result)
        raise SubscriptionParseError(f"unsupported T4 module: {module}")

    def _normalize_vod(
        self,
        vod: dict[str, Any],
        source: SourceConfig,
        plugin: PluginDescriptor,
        *,
        module: str = "",
    ) -> dict[str, Any]:
        item = dict(vod)
        if not item.get("vod_pic"):
            for field in ("pic", "poster", "cover", "thumb", "image"):
                if isinstance(item.get(field), str) and item[field]:
                    item["vod_pic"] = item[field]
                    break
        raw_id = str(item.get("vod_id") or "")
        if raw_id:
            key = (source.source_id, plugin.plugin_id, raw_id)
            if module == "detail":
                cached = self._catalog_metadata.get(key) or {}
                for field in ("vod_name", "vod_pic"):
                    if not item.get(field) and cached.get(field):
                        item[field] = cached[field]
            elif module in {"home", "category", "search"}:
                cached = self._catalog_metadata.get(key) or {}
                cached.update({field: item[field] for field in ("vod_name", "vod_pic") if item.get(field)})
                self._catalog_metadata[key] = cached
                self._catalog_metadata.move_to_end(key)
                while len(self._catalog_metadata) > 512:
                    self._catalog_metadata.popitem(last=False)
        if raw_id and plugin.api == "csp_PianDan":
            key = (source.source_id, plugin.plugin_id, raw_id)
            self._navigation_cards[key] = dict(item)
            self._navigation_cards.move_to_end(key)
            while len(self._navigation_cards) > 512:
                self._navigation_cards.popitem(last=False)
        if raw_id:
            item["vod_id"] = self._opaque_ids.put(
                OpaqueValue(source.source_id, plugin.plugin_id, "detail", raw_id, trace_id())
            )
        play_urls = item.get("vod_play_url")
        if isinstance(play_urls, str):
            item["vod_play_url"] = self._normalize_play_urls(
                play_urls,
                source,
                plugin,
            )
        return item

    def _normalize_play_urls(
        self,
        value: str,
        source: SourceConfig,
        plugin: PluginDescriptor,
    ) -> str:
        groups = []
        for group in value.split("$$$"):
            episodes = []
            for episode in group.split("#"):
                label, separator, raw_id = episode.partition("$")
                if not separator and plugin.api in {"csp_TgWeb", "csp_TgChannel", "csp_TgDouBan"}:
                    parsed = urlsplit(episode)
                    if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.path.startswith("/v/"):
                        label, separator, raw_id = "视频", "$", episode
                if not separator or not raw_id:
                    episodes.append(episode)
                    continue
                opaque = self._opaque_ids.put(
                    OpaqueValue(source.source_id, plugin.plugin_id, "play", raw_id, trace_id())
                )
                episodes.append(f"{label}${opaque}")
            groups.append("#".join(episodes))
        return "$$$".join(groups)

    @staticmethod
    def _normalize_play(result: dict[str, Any]) -> dict[str, Any]:
        payload = dict(result)
        payload.setdefault("parse", 0)
        payload.setdefault("jx", 0)
        payload.setdefault("header", {})
        headers = payload["header"]
        if headers in (None, ""):
            headers = {}
        elif isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except ValueError as exc:
                raise SubscriptionParseError("播放请求头不是有效的 JSON 对象") from exc
        if not isinstance(headers, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in headers.items()
        ):
            raise SubscriptionParseError("播放请求头必须是字符串键值对象")
        payload["header"] = {key: value.strip(" \t") for key, value in headers.items()}
        for key in ("parse", "jx"):
            if payload[key] in ("0", "1"):
                payload[key] = int(payload[key])
        return payload

    def unwrap_id(
        self,
        value: str,
        *,
        source_id: str,
        plugin_id: str,
        kind: str,
    ) -> str:
        resolved = self._opaque_ids.get(
            value,
            source_id=source_id,
            plugin_id=plugin_id,
            kind=kind,
        )
        if resolved is not None:
            record("id.resolved", parent_trace=self._opaque_ids.origin_trace(value))
        elif value.startswith("t4_"):
            raise SubscriptionParseError("播放标识已失效，请重新打开详情")
        return resolved or value


class AtvpWorkerInvoker:
    def __init__(self, pool: Any, proxies: PluginProxyRegistry | None = None) -> None:
        self._pool = pool
        self.proxies = proxies or PluginProxyRegistry()
        self._proxy_endpoints: dict[str, tuple[str, str]] = {}
        self._initialized: dict[str, str] = {}
        self._state_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._browse = BrowseContextStore()
        self._generations: dict[str, int] = {}

    def _lock_for(self, runtime_key: str) -> threading.Lock:
        with self._state_lock:
            return self._locks.setdefault(runtime_key, threading.Lock())

    def reset(self, runtime_key: str) -> None:
        with self._lock_for(runtime_key):
            self._initialized.pop(runtime_key, None)
            self._remove_proxy(runtime_key)
            self._pool.restart(runtime_key)
            self._browse.remove(runtime_key)
            self._generations.pop(runtime_key, None)

    def _remove_proxy(self, runtime_key: str) -> None:
        endpoint = self._proxy_endpoints.pop(runtime_key, None)
        if endpoint:
            self.proxies.remove(endpoint[1])

    def _sync_proxy(self, runtime_key: str, endpoint: str | None = None) -> None:
        current_endpoint = getattr(self._pool, "proxy_endpoint", None)
        if current_endpoint is not None:
            endpoint = current_endpoint(runtime_key) or endpoint
        previous = self._proxy_endpoints.get(runtime_key)
        if endpoint and (previous is None or previous[0] != endpoint):
            self._remove_proxy(runtime_key)
            key = self.proxies.register(endpoint, runtime_key, trace_id())
            self._proxy_endpoints[runtime_key] = (endpoint, key)

    def close(self) -> None:
        """Close an idle batch runtime, including workers created by navigation."""
        self._pool.close()
        with self._state_lock:
            for runtime_key in tuple(self._proxy_endpoints):
                self._remove_proxy(runtime_key)
            self._initialized.clear()
            self._locks.clear()
            self._generations.clear()
        self._browse.close()

    def call(
        self,
        runtime_key: str,
        plugin: PluginDescriptor,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        lease = getattr(self._pool, "lease", None)
        with self._lock_for(runtime_key), (lease(runtime_key) if lease else nullcontext()):
            params = dict(params)
            base = str(params.pop("_proxy_base", "")).rstrip("/")
            extend = plugin.worker_extend(server_proxy=bool(params.pop("_server_proxy", False)))
            previous = self._initialized.get(runtime_key)
            has_session = getattr(self._pool, "has_session", None)
            cold = previous == extend and has_session is not None and not has_session(runtime_key)
            if previous != extend or cold:
                self._initialized.pop(runtime_key, None)
                self._remove_proxy(runtime_key)
                if previous is not None and not cold:
                    self._pool.restart(runtime_key)
                    self._browse.remove(runtime_key)
                initialized = self._pool.request(
                    runtime_key,
                    "init",
                    {"extend": extend, "site_key": runtime_key},
                )
                self._sync_proxy(runtime_key, (initialized or {}).get("proxy_url"))
                self._initialized[runtime_key] = extend
                self._generations[runtime_key] = self._generations.get(runtime_key, 0) + 1
            try:
                generation = self._generations[runtime_key]
                if method in {"detail", "category"}:
                    ids = (
                        (params.get("ids") or []) if method == "detail"
                        else [params.get("tid", "")]
                    )
                    contexts = self._browse.for_detail(runtime_key, ids, generation, kind=method)
                    for browse_method, browse_params in contexts:
                        self._pool.request(runtime_key, browse_method, browse_params)
                    if contexts:
                        self._browse.restored(runtime_key, ids, generation, kind=method)
                        record("worker.context_restored", method=method, count=len(contexts))
                result = self._pool.request(runtime_key, method, params)
                self._browse.remember(runtime_key, method, params, result, generation)
                self._sync_proxy(runtime_key)
                endpoint = self._proxy_endpoints.get(runtime_key)
                if endpoint:
                    internal, key = endpoint
                    result = rewrite_proxy_urls(result, internal, base + "/t4/proxy/" + key)
                return result
            except Exception:
                self._initialized.pop(runtime_key, None)
                self._remove_proxy(runtime_key)
                raise


def _bool_param(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _json_or_text(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return ""
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return str(value)


def _filter_map(params: dict[str, Any]) -> dict[str, Any]:
    candidates = (params.get("filters"), params.get("filter"), params.get("extend"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            return dict(candidate)
        if isinstance(candidate, str) and candidate.strip():
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                return decoded

    values: dict[str, Any] = {}
    for key, value in params.items():
        if key.startswith("filter.") and value not in (None, ""):
            values[key[7:]] = value
    return values
