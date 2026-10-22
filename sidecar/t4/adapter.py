from __future__ import annotations

import json
from dataclasses import dataclass
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


class WorkerInvoker(Protocol):
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


class T4Adapter:
    def __init__(
        self,
        source_registry: SourceRegistry,
        worker_invoker: WorkerInvoker,
        *,
        subscription_fetcher: SubscriptionFetcher | None = None,
        opaque_ids: OpaqueIdStore | None = None,
    ) -> None:
        self._sources = source_registry
        self._worker = worker_invoker
        self._fetcher = subscription_fetcher
        self._opaque_ids = opaque_ids or OpaqueIdStore()

    async def dispatch(self, request: T4Request) -> dict[str, Any]:
        source = self._sources.get(request.source_id)
        if source is None:
            raise SubscriptionParseError("source was not found")
        snapshot = await resolve_subscription(source, self._fetcher)
        plugin = snapshot.plugin_for(request.plugin_id)
        method, params = self._translate(request.module, request.params)
        params = self._unwrap_params(
            request.module,
            params,
            source.source_id,
            plugin.plugin_id,
        )
        result = self._worker.call(
            f"{request.source_id}:{plugin.plugin_id}",
            plugin,
            method,
            params,
        )
        return self._normalize(
            request.module,
            result,
            source,
            plugin,
        )

    def _unwrap_params(
        self,
        module: str,
        params: dict[str, Any],
        source_id: str,
        plugin_id: str,
    ) -> dict[str, Any]:
        output = dict(params)
        if module == "detail":
            output["ids"] = [
                self.unwrap_id(
                    str(value),
                    source_id=source_id,
                    plugin_id=plugin_id,
                    kind="detail",
                )
                for value in output.get("ids", [])
            ]
        elif module == "play":
            output["id"] = self.unwrap_id(
                str(output.get("id") or ""),
                source_id=source_id,
                plugin_id=plugin_id,
                kind="play",
            )
        return output

    @staticmethod
    def _translate(module: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if module == "home":
            return "home", {"filter": _bool_param(params.get("filter"), True)}
        if module == "category":
            return (
                "category",
                {
                    "tid": str(params.get("tid") or params.get("t") or ""),
                    "pg": str(params.get("pg") or "1"),
                    "filter": _bool_param(params.get("filter"), True),
                    "extend": _json_or_text(params.get("extend", "")),
                },
            )
        if module == "search":
            return (
                "search",
                {
                    "key": str(params.get("wd") or params.get("key") or ""),
                    "quick": _bool_param(params.get("quick"), True),
                    "pg": str(params.get("pg") or "1"),
                },
            )
        if module == "detail":
            raw_ids = params.get("ids") or params.get("id") or ""
            ids = (
                raw_ids
                if isinstance(raw_ids, list)
                else [value for value in str(raw_ids).split(",") if value]
            )
            return "detail", {"ids": ids}
        if module == "play":
            return (
                "play",
                {
                    "flag": str(params.get("flag") or ""),
                    "id": str(params.get("play") or params.get("id") or ""),
                    "vip_flags": _json_or_text(params.get("vipFlags", [])),
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
        if module == "play":
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
        if raw_id:
            item["vod_id"] = self._opaque_ids.put(
                OpaqueValue(source.source_id, plugin.plugin_id, "detail", raw_id)
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
                if not separator:
                    episodes.append(episode)
                    continue
                opaque = self._opaque_ids.put(
                    OpaqueValue(source.source_id, plugin.plugin_id, "play", raw_id)
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
        return payload

    def unwrap_id(
        self,
        value: str,
        *,
        source_id: str,
        plugin_id: str,
        kind: str,
    ) -> str:
        return self._opaque_ids.get(
            value,
            source_id=source_id,
            plugin_id=plugin_id,
            kind=kind,
        ) or value


class AtvpWorkerInvoker:
    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._initialized: set[str] = set()

    def call(
        self,
        runtime_key: str,
        plugin: PluginDescriptor,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        if runtime_key not in self._initialized:
            self._pool.request(runtime_key, "init", {"extend": plugin.worker_extend()})
            self._initialized.add(runtime_key)
        try:
            return self._pool.request(runtime_key, method, params)
        except Exception:
            self._initialized.discard(runtime_key)
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
