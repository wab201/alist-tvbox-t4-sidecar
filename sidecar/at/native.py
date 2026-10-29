from __future__ import annotations

import base64
import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from sidecar.at.subscription import PluginDescriptor, _builtin_route
from sidecar.diagnostics import record


class NativeAtError(RuntimeError):
    """A redacted failure while calling an AT native source."""

    def __init__(self, message: str, *, status: int | None = None, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason

    def public_error(self) -> dict[str, Any]:
        return {
            "code": "at_upstream_failed",
            "message": "AT 上游请求失败，请查看链路日志中的状态和原因",
            **({"upstream_status": self.status} if self.status is not None else {}),
            **({"reason": self.reason} if self.reason else {}),
        }


class NativeAtUnsupportedError(NativeAtError):
    """The original source has no server-side route that T4 can invoke."""


class NativeAtClient:
    """Calls AT-owned builtin and remote plugin routes without running their code."""

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
        if plugin.t4_endpoint and plugin.plugin_type == "official_t4":
            if method == "action":
                raise NativeAtUnsupportedError(
                    "official AT plugin endpoint does not expose standalone actions"
                )
            return self._request(
                plugin, "/t4/v", self._t4_query(method, params),
                {"X-CLIENT": str(params.get("client") or "t4-sidecar")},
                endpoint=plugin.t4_endpoint,
            )
        if plugin.plugin_type != "builtin":
            raise NativeAtError("native bridge requires an AT-owned source")
        if not _builtin_route(plugin.api):
            raise NativeAtUnsupportedError(
                "builtin source has no server-side T4 route"
            )
        if plugin.api == "csp_PianDan" and method in {"detail", "play", "action"}:
            raise NativeAtUnsupportedError(
                "PianDan is title-search navigation; select a card in the client "
                "to search other sources, not a standalone detail or play API"
            )
        if plugin.t4_endpoint:
            query = self._t4_query(method, params)
            headers = {"X-CLIENT": str(params.get("client") or "t4-sidecar")}
            return self._request(
                plugin, "/t4/v", query, headers, endpoint=plugin.t4_endpoint,
            )
        if plugin.api == "csp_Live" and method == "play":
            play_id = str(params.get("id") or "")
            parsed = urlsplit(play_id)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                # LiveService publishes the final stream in detail; /live-play
                # is only a placeholder in AT, not its URL resolver.
                record("at.inline_media", route="live", scheme=parsed.scheme)
                return {"parse": 0, "jx": 0, "url": play_id, "header": {}}
        if plugin.api in {"csp_TgWeb", "csp_TgChannel", "csp_TgDouBan"} and method == "play":
            play_id = str(params.get("id") or "")
            parsed = urlsplit(play_id)
            if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.path.startswith("/v/"):
                record("at.inline_media", route="telegram", scheme=parsed.scheme)
                return {"parse": 0, "jx": 0, "url": play_id, "header": {}}
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

        # Unknown players must not be advertised to AT as DASH-capable FongMi.
        default_client = "t4-sidecar" if method == "play" else "com.fongmi.android.tv"
        headers = {"X-CLIENT": str(params.get("client") or default_client)}
        payload = self._request(plugin, path, query, headers)
        if plugin.api == "csp_PianDan" and method == "home":
            recommendations = self._request(plugin, path, {"t": "0"}, headers)
            payload["list"] = recommendations.get("list", [])
        return payload

    def _request(
        self,
        plugin: PluginDescriptor,
        path: str,
        query: dict[str, Any],
        headers: dict[str, str],
        *,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        endpoint = endpoint or self._endpoint(plugin, path)
        start = time.monotonic()
        try:
            response = self._client.get(endpoint, params=query, headers=headers)
        except httpx.HTTPError as exc:
            record("at.failed", route=path.strip("/"), error=type(exc).__name__,
                   duration_ms=round((time.monotonic() - start) * 1000))
            raise NativeAtError(
                "AT native request failed before receiving a response", reason="network_error",
            ) from exc
        record("at.response", route=path.strip("/"), status=response.status_code,
               duration_ms=round((time.monotonic() - start) * 1000),
               bytes=len(response.content))
        if response.status_code < 200 or response.status_code >= 300:
            reason = "http_status"
            try:
                error = response.json()
                message = str(error.get("detail") or error.get("message") or "") if isinstance(error, dict) else ""
                # Classify only known messages; never expose upstream body or URLs.
                for marker, category in (
                    ("Invalid share link format", "invalid_share"),
                    ("无法识别的分享链接", "unsupported_share"),
                    ("帐号", "provider_account"),
                    ("账号", "provider_account"),
                    ("找不到", "resource_not_found"),
                    ("失效", "share_unavailable"),
                    ("不存在", "resource_not_found"),
                    ("过期", "expired"),
                ):
                    if marker in message:
                        reason = category
                        break
            except ValueError:
                pass
            record("at.rejected", route=path.strip("/"), status=response.status_code, reason=reason)
            raise NativeAtError(
                f"AT native request returned status {response.status_code}",
                status=response.status_code, reason=reason,
            )
        if path == "/pansou-group" and query == {"t": "0"} and not response.content.strip():
            # The native grouped-search controller has no home recommendations.
            return {"class": [], "list": []}
        try:
            payload = response.json()
        except ValueError as exc:
            raise NativeAtError("AT native response is not JSON", reason="invalid_response") from exc
        if not isinstance(payload, dict):
            raise NativeAtError("AT native response must be an object", reason="invalid_response")
        return payload

    @staticmethod
    def _t4_query(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "home":
            return {"filter": str(params.get("filter", True)).lower()}
        if method == "category":
            filters = dict(params.get("native_filter") or {})
            for key in ("sort", "year", "genre", "region", "platform"):
                if params.get(key) not in (None, ""):
                    filters[key] = params[key]
            encoded = base64.urlsafe_b64encode(
                json.dumps(filters, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).decode("ascii").rstrip("=")
            return {
                "ac": "detail", "t": str(params.get("tid") or ""),
                "pg": str(params.get("pg") or "1"), "ext": encoded,
            }
        if method == "search":
            return {
                "wd": str(params.get("key") or ""),
                "pg": str(params.get("pg") or "1"),
                "quick": str(params.get("quick", True)).lower(),
            }
        if method == "detail":
            ids = params.get("ids") or []
            if not isinstance(ids, list):
                ids = [ids]
            return {"ac": "detail", "ids": ",".join(str(value) for value in ids)}
        if method in {"play", "action"}:
            play_id = str(params.get("id") or "")
            if not play_id:
                raise NativeAtUnsupportedError("official T4 play/action requires a play id")
            # AT dispatches action tracks (including Live follow) through play.
            return {"play": play_id, "flag": str(params.get("flag") or "")}
        raise NativeAtUnsupportedError("unsupported official T4 method")

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
            if plugin.api == "csp_AList":
                return {"type": "0"}
            if route in {"/pansou", "/pansou-group"}:
                return {"t": "0"}
            if route == "/tg-search" and plugin.api == "csp_TgWeb":
                return {"web": "true"}
            return {}
        if method == "category":
            query = {
                "t": str(params.get("tid") or ""),
                "pg": str(params.get("pg") or "1"),
            }
            if route in {"/pansou", "/pansou-group"} and not query["t"]:
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
                if plugin.api == "csp_AList":
                    query["type"] = "0"
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
            if plugin.api == "csp_AList":
                query["type"] = "0"
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
            if route in {"/pansou", "/pansou-group", "/media"}:
                return {"id": values[0]} if values else {"id": ""}
            if route in {"/tg-search", "/tgsc", "/tg-db"}:
                query = {"id": values[0]} if values else {"id": ""}
                for key in ("ac", "title"):
                    # T4's action is not AT's proxy-playlist representation.
                    if key == "ac" and params.get(key) == "detail":
                        continue
                    if params.get(key) not in (None, ""):
                        query[key] = str(params[key])
                return query
            query = {"ids": ",".join(values)}
            for key in ("ac", "depth"):
                # /vod's ac changes playback IDs into proxy URLs, not T4 actions.
                if route == "/vod" and key == "ac":
                    continue
                if params.get(key) not in (None, ""):
                    query[key] = str(params[key])
            if route == "/live" and params.get("platform") not in (None, ""):
                query["platform"] = str(params["platform"])
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
        if route == "/pian-dan":
            for key, item in value.items():
                if item is not None and item != "" and key not in {
                    "t", "pg", "ac", "wd", "quick",
                }:
                    query[key] = str(item)
        elif route in {"/bilibili", "/media"}:
            for key, item in value.items():
                if item is not None and item != "":
                    query[f"filter.{key}"] = str(item)

    @staticmethod
    def _filter_text(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def _play_query(
        cls,
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
            "type": (
                "" if params.get("_server_proxy") and cls._play_route(plugin) == "/play"
                else str(params.get("type") or "client-proxy")
            ),
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
