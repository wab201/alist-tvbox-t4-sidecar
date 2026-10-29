from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
from dataclasses import dataclass
import threading
import time
import re
from typing import Any, Protocol

from sidecar.at.subscription import (
    PluginDescriptor,
    SubscriptionParseError,
    SubscriptionFetcher,
    SubscriptionSnapshot,
    resolve_subscription,
)
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.opaque import OpaqueIdStore, OpaqueValue
from sidecar.diagnostics import record, trace_id, trace_scope
from sidecar.plugin_proxy import PluginProxyRegistry, rewrite_proxy_urls
from sidecar.playback import PlaybackBridge


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
    ) -> None:
        self._sources = source_registry
        self._worker = worker_invoker
        self._native = native_invoker
        self._fetcher = subscription_fetcher
        self._opaque_ids = opaque_ids or OpaqueIdStore()
        self._subscription_resolver = subscription_resolver
        self._local_snapshot_provider = local_snapshot_provider
        self._playback = playback_bridge
        self._plugin_locks: dict[str, asyncio.Lock] = {}
        self._navigation_cards: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        self._navigation_slots = asyncio.Semaphore(3)

    def plugin_lock(self, source_id: str, plugin_id: str) -> asyncio.Lock:
        key = f"{source_id}:{plugin_id}"
        return self._plugin_locks.setdefault(key, asyncio.Lock())

    async def reset_plugin(self, source_id: str, plugin_id: str) -> None:
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

        candidates = [
            item for item in snapshot.plugins
            if item.api not in {"csp_PianDan", "csp_Push"}
            and item.site_options.get("searchable", 1) not in {0, "0", False}
            and item.public_metadata().get("t4_supported", True)
        ][:6]

        async def find(target: PluginDescriptor):
            async with self._navigation_slots:
                try:
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
                    if match is None:
                        return None
                    detail = await self.dispatch(T4Request(
                        "detail", source.source_id, target.plugin_id,
                        {"ids": match["vod_id"], "client": request.params.get("client", "")},
                        request.proxy_base,
                    ))
                    return target, next(iter(detail.get("list") or []), {})
                except Exception as exc:
                    record("navigation.source_failed", plugin=target.plugin_id,
                           error=type(exc).__name__)
                    return None

        record("navigation.search", count=len(candidates))
        results = await asyncio.gather(*(find(target) for target in candidates))
        flags, groups = [], []
        for result in results:
            if result is None:
                continue
            target, vod = result
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
            raise SubscriptionParseError("当前优先检索的源中未找到同名可播资源，请使用客户端全局搜索")
        item = dict(card)
        item.update(
            vod_id=str(ids[0]), vod_play_from="$$$".join(flags),
            vod_play_url="$$$".join(groups),
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
            payload = dict(result)
            raw_list = payload.get("list")
            if isinstance(raw_list, list):
                payload["list"] = [
                    self._normalize_vod(item, source, plugin)
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
    ) -> dict[str, Any]:
        item = dict(vod)
        raw_id = str(item.get("vod_id") or "")
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
        payload["header"] = headers
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

    def _lock_for(self, runtime_key: str) -> threading.Lock:
        with self._state_lock:
            return self._locks.setdefault(runtime_key, threading.Lock())

    def reset(self, runtime_key: str) -> None:
        with self._lock_for(runtime_key):
            self._initialized.pop(runtime_key, None)
            self._remove_proxy(runtime_key)
            self._pool.restart(runtime_key)

    def _remove_proxy(self, runtime_key: str) -> None:
        endpoint = self._proxy_endpoints.pop(runtime_key, None)
        if endpoint:
            self.proxies.remove(endpoint[1])

    def close(self) -> None:
        """Close an idle batch runtime, including workers created by navigation."""
        self._pool.close()
        with self._state_lock:
            for runtime_key in tuple(self._proxy_endpoints):
                self._remove_proxy(runtime_key)
            self._initialized.clear()
            self._locks.clear()

    def call(
        self,
        runtime_key: str,
        plugin: PluginDescriptor,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        with self._lock_for(runtime_key):
            params = dict(params)
            base = str(params.pop("_proxy_base", "")).rstrip("/")
            extend = plugin.worker_extend(server_proxy=bool(params.pop("_server_proxy", False)))
            previous = self._initialized.get(runtime_key)
            if previous != extend:
                self._initialized.pop(runtime_key, None)
                self._remove_proxy(runtime_key)
                if previous is not None:
                    self._pool.restart(runtime_key)
                initialized = self._pool.request(
                    runtime_key,
                    "init",
                    {"extend": extend, "site_key": runtime_key},
                )
                endpoint = (initialized or {}).get("proxy_url")
                if endpoint:
                    key = self.proxies.register(endpoint, runtime_key, trace_id())
                    self._proxy_endpoints[runtime_key] = (endpoint, key)
                self._initialized[runtime_key] = extend
            try:
                result = self._pool.request(runtime_key, method, params)
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
