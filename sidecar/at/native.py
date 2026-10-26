from __future__ import annotations

import json
from typing import Any

import httpx

from sidecar.at.subscription import PluginDescriptor, _builtin_route


class NativeAtError(RuntimeError):
    """A redacted failure while calling an AT native source."""


class NativeAtUnsupportedError(NativeAtError):
    """The original source has no server-side route that T4 can invoke."""


class NativeAtClient:
    """Calls AT's own built-in source and playback routes without reimplementing them."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        timeout: float = 15.0,
    ) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def call(
        self,
        plugin: PluginDescriptor,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if plugin.plugin_type != "builtin":
            raise NativeAtError("native bridge requires a builtin plugin")
        if not _builtin_route(plugin.api):
            raise NativeAtUnsupportedError(
                "builtin source has no server-side T4 route"
            )
        if method in {"play", "action"}:
            path = self._play_route(plugin) if method == "play" else "/play"
            query = self._play_query(plugin, params)
        else:
            path = _builtin_route(plugin.api)
            if not path:
                raise NativeAtUnsupportedError(
                    "builtin source has no server-side T4 route"
                )
            query = self._catalog_query(plugin, method, params)

        endpoint = self._endpoint(plugin, path)
        headers = {"X-CLIENT": str(params.get("client") or "com.fongmi.android.tv")}
        try:
            response = self._client.get(endpoint, params=query, headers=headers)
        except httpx.HTTPError as exc:
            raise NativeAtError("AT native request failed before receiving a response") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise NativeAtError(f"AT native request returned status {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise NativeAtError("AT native response is not JSON") from exc
        if not isinstance(payload, dict):
            raise NativeAtError("AT native response must be an object")
        return payload

    @staticmethod
    def _play_route(plugin: PluginDescriptor) -> str:
        return {
            "csp_Emby": "/emby-play",
            "csp_Jellyfin": "/jellyfin-play",
            "csp_FeiNiu": "/feiniu-play",
            "csp_Live": "/live-play",
        }.get(plugin.api, "/play")

    @staticmethod
    def _endpoint(plugin: PluginDescriptor, path: str) -> str:
        base = str(plugin.backend_api or "").rstrip("/")
        if not base:
            raise NativeAtError("AT backend api is missing")
        token = str(plugin.token or "").strip()
        suffix = "" if not token or token == "-" else f"/{token}"
        return f"{base}{path}{suffix}"

    @classmethod
    def _catalog_query(
        cls,
        plugin: PluginDescriptor,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        route = _builtin_route(plugin.api)
        if method == "home":
            if route == "/pansou":
                return {"t": "0"}
            if route == "/tg-search" and plugin.api == "csp_TgWeb":
                return {"web": "true"}
            return {}
        if method == "category":
            query = {
                "t": str(params.get("tid") or ""),
                "pg": str(params.get("pg") or "1"),
            }
            if route == "/pansou" and not query["t"]:
                query["t"] = "0"
            if route == "/tg-search" and plugin.api == "csp_TgWeb":
                query["web"] = "true"
                if query["t"] == "0":
                    query["size"] = "5"
            elif route == "/tgsc":
                query["size"] = str(
                    params.get("size")
                    or ("120" if query["t"] == "0" else "30")
                )
            elif route == "/tg-db":
                query["size"] = str(params.get("size") or "30")
            cls._add_filter_query(query, route, params.get("native_filter"))
            if route == "/vod":
                query["f"] = cls._filter_text(params.get("native_filter"))
            for key in (
                "sort",
                "size",
                "depth",
                "type",
                "ac",
                "year",
                "genre",
                "region",
                "platform",
            ):
                if params.get(key) not in (None, ""):
                    query[key] = str(params[key])
            return query
        if method == "search":
            query = {
                "wd": str(params.get("key") or ""),
                "pg": str(params.get("pg") or "1"),
                "quick": str(params.get("quick", True)).lower(),
            }
            if route == "/tg-search" and plugin.api == "csp_TgWeb":
                query["web"] = "true"
            elif route == "/tgsc":
                query["size"] = str(params.get("size") or "30")
            elif route == "/tg-db":
                query["size"] = "20"
            cls._add_filter_query(query, route, params.get("native_filter"))
            return query
        if method == "detail":
            ids = params.get("ids") or []
            if not isinstance(ids, list):
                ids = [ids]
            values = [str(value) for value in ids if str(value)]
            if route in {"/pansou", "/media"}:
                return {"id": values[0]} if values else {"id": ""}
            if route in {"/tg-search", "/tgsc", "/tg-db"}:
                query = {"id": values[0]} if values else {"id": ""}
                for key in ("ac", "title"):
                    if params.get(key) not in (None, ""):
                        query[key] = str(params[key])
                return query
            query = {"ids": ",".join(values)}
            for key in ("ac", "depth"):
                if params.get(key) not in (None, ""):
                    query[key] = str(params[key])
            return query
        raise NativeAtError(f"unsupported native method: {method}")

    @classmethod
    def _add_filter_query(
        cls,
        query: dict[str, Any],
        route: str | None,
        value: Any,
    ) -> None:
        if not isinstance(value, dict):
            return
        if route in {"/bilibili", "/media"}:
            for key, item in value.items():
                if item is not None and item != "":
                    query[f"filter.{key}"] = str(item)

    @staticmethod
    def _filter_text(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _play_query(
        plugin: PluginDescriptor,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        play_id = str(params.get("id") or "")
        if not play_id:
            raise NativeAtError("native play id is required")
        if plugin.api == "csp_BiliBili":
            query: dict[str, Any] = {"bvid": play_id}
            if "dash" in params:
                query["dash"] = str(params.get("dash")).lower()
            return query
        if plugin.api == "csp_Live":
            return {"id": play_id}
        return {
            "id": play_id,
            "from": str(params.get("from") or "jar"),
            "type": str(params.get("type") or "client-proxy"),
            **(
                {"t": str(params["t"])}
                if params.get("t") not in (None, "")
                else {}
            ),
        }


class NativeAtInvoker:
    def __init__(self, client: NativeAtClient) -> None:
        self._client = client

    def call(
        self,
        runtime_key: str,
        plugin: PluginDescriptor,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        del runtime_key
        return self._client.call(plugin, method, params)

    def close(self) -> None:
        self._client.close()
