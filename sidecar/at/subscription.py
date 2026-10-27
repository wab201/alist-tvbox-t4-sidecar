from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

import httpx

from sidecar.gateway.source_registry import SourceConfig


class SubscriptionParseError(ValueError):
    """Raised when an AT subscription violates the observed response contract."""


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    plugin_id: str
    name: str
    plugin_type: str
    api: str
    source: str
    loader: str
    raw: bool
    token: str
    secret: str
    local_proxy_config: Any
    data: Any
    filters: tuple[dict[str, Any], ...]
    backend_api: str = ""
    playback_token: str = ""
    playback_config_url: str = ""
    uid: str = ""
    home_page: str = ""
    site_options: dict[str, Any] = field(default_factory=dict)
    native_client_site: dict[str, Any] = field(default_factory=dict)

    def worker_extend(self) -> str:
        payload: dict[str, Any] = {
            "api": self.backend_api or self.api,
            "source": self.source,
            "loader": self.loader,
            "raw": self.raw,
            "token": self.token,
            "secret": self.secret,
            "local_proxy_config": self.local_proxy_config,
        }
        if self.data is not None:
            payload["data"] = self.data
        if self.filters:
            payload["filters"] = list(self.filters)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return base64.b64encode(raw.encode("utf-8")).decode("ascii")

    def public_metadata(self) -> dict[str, Any]:
        metadata = {
            "id": self.plugin_id,
            "name": self.name,
            "type": self.plugin_type,
            "api": self.api,
            "raw": self.raw,
            "filters": len(self.filters),
        }
        if self.plugin_type == "builtin":
            route = _builtin_route(self.api)
            if route:
                metadata["native_route"] = route
                capabilities = ["home", "category", "search", "detail", "play"]
                if self.api == "csp_PianDan":
                    metadata["delegated_capabilities"] = ["detail", "play"]
                    metadata["compatibility_mode"] = "title_search_navigation"
                elif self.api == "csp_Media":
                    capabilities.append("action")
                metadata["capabilities"] = capabilities
            elif self.api == "csp_Push":
                metadata.update(
                    {
                        "t4_supported": False,
                        "client_supported": bool(self.native_client_site),
                        "compatibility_mode": "native_client_passthrough",
                        "capabilities": [],
                        "unsupported_reason": (
                            "AT Push runs in the original client via the unified "
                            "subscription, not a standalone server T4 API"
                        ),
                    }
                )
            elif self.api in {"csp_WebHome", "csp_Nostr"}:
                metadata.update(
                    {
                        "t4_supported": False,
                        "compatibility_mode": "page_entry_only",
                        "page_entry": bool(self.home_page),
                        "capabilities": [],
                        "unsupported_reason": (
                            "this source is a page entry, not a standard AT VOD service"
                        ),
                    }
                )
            else:
                metadata.update(
                    {
                        "t4_supported": False,
                        "compatibility_mode": "unmapped_builtin",
                        "capabilities": [],
                        "unsupported_reason": (
                            "the subscription contains a builtin source without "
                            "a known AT server route"
                        ),
                    }
                )
        elif self.plugin_type not in {"raw_python", "secspider", "javascript"}:
            metadata.update(t4_supported=False, capabilities=[],
                            compatibility_mode="unsupported_runtime")
        return metadata


@dataclass(frozen=True, slots=True)
class SubscriptionSnapshot:
    source_id: str
    site_count: int
    plugins: tuple[PluginDescriptor, ...]
    errors: tuple[str, ...] = ()
    unresolved_sites: tuple[dict[str, Any], ...] = ()

    def public_metadata(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "site_count": self.site_count,
            "plugins": [plugin.public_metadata() for plugin in self.plugins],
            "errors": list(self.errors),
        }

    def plugin_for(self, plugin_id: str | None) -> PluginDescriptor:
        if plugin_id:
            for plugin in self.plugins:
                if (
                    plugin.plugin_id == plugin_id
                    or plugin.name == plugin_id
                    or plugin.api == plugin_id
                ):
                    return plugin
            raise SubscriptionParseError("requested plugin was not found")
        if len(self.plugins) == 1:
            return self.plugins[0]
        if not self.plugins:
            raise SubscriptionParseError("subscription contains no executable plugin")
        raise SubscriptionParseError("plugin selection is required")


def _decode_json_text(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass

    compact = "".join(value.split())
    if not compact:
        raise SubscriptionParseError("ext is empty")
    try:
        decoded = base64.b64decode(compact + "=" * (-len(compact) % 4))
    except (binascii.Error, ValueError) as exc:
        raise SubscriptionParseError("ext is neither JSON nor base64 JSON") from exc
    try:
        return json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubscriptionParseError("base64 ext does not contain JSON") from exc


def decode_ext(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        raise SubscriptionParseError("site ext must be an object or string")

    payload = _decode_json_text(value.strip())
    if not isinstance(payload, dict):
        raise SubscriptionParseError("decoded ext must be an object")
    return payload


def _origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SubscriptionParseError("AT URL must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise SubscriptionParseError("AT URL must not contain credentials")
    return (
        parsed.scheme.lower(),
        parsed.hostname.lower(),
        parsed.port,
    )


def _origin_url(value: str) -> str:
    scheme, hostname, port = _origin(value)
    host = f"[{hostname}]" if ":" in hostname else hostname
    suffix = f":{port}" if port is not None else ""
    return f"{scheme}://{host}{suffix}"


def _same_origin(left: str, right: str) -> bool:
    return _origin(left) == _origin(right)


def _validate_http_url(value: str, *, label: str) -> None:
    try:
        _origin(value)
    except (SubscriptionParseError, ValueError) as exc:
        raise SubscriptionParseError(f"{label} must be an absolute http(s) URL") from exc


def _validate_loader(loader: str, at_base_url: str) -> None:
    if not loader:
        return
    if not _same_origin(loader, at_base_url):
        raise SubscriptionParseError("Atvp loader is outside the configured AT origin")
    path = urlsplit(loader).path
    normalized_path = path.lower()
    if normalized_path != "/atvp.py" and not (
        normalized_path.startswith("/atvp/") and normalized_path.endswith(".py")
    ):
        raise SubscriptionParseError("Atvp loader path is not an AT dynamic loader")


def _classify_plugin(api: str, source: str, raw: bool, loader: str) -> str:
    if raw or source.lower().split("?", 1)[0].endswith(".py"):
        return "raw_python"
    if source.lower().split("?", 1)[0].endswith(".txt"):
        return "secspider"
    if api == "csp_PyProxy" and source and loader:
        return "secspider"
    if source or loader:
        return "unsupported_plugin"
    if not api or api.startswith("csp_"):
        return "builtin"
    return "unsupported_plugin"


def _builtin_route(api: str) -> str | None:
    return {
        "csp_BiliBili": "/bilibili",
        "csp_Media": "/media",
        "csp_AList": "/vod",
        "csp_XiaoYa": "/vod",
        "csp_PianDan": "/pian-dan",
        "csp_Live": "/live",
        "csp_Emby": "/emby",
        "csp_Jellyfin": "/jellyfin",
        "csp_FeiNiu": "/feiniu",
        "csp_FishPanSou": "/pansou",
        "csp_FishPanSouGroup": "/pansou",
        "csp_TgDouBan": "/tg-db",
        "csp_TgChannel": "/tgsc",
        "csp_TgWeb": "/tg-search",
    }.get(api)


class SubscriptionParser:
    def __init__(self, at_base_url: str) -> None:
        self._at_base_url = at_base_url.rstrip("/")
        _origin(self._at_base_url)

    def parse(self, source_id: str, payload: Any) -> SubscriptionSnapshot:
        if not isinstance(payload, dict):
            raise SubscriptionParseError("subscription response must be an object")
        raw_sites = payload.get("sites", [])
        if not isinstance(raw_sites, list):
            raise SubscriptionParseError("subscription sites must be a list")

        plugins: list[PluginDescriptor] = []
        errors: list[str] = []
        unresolved: list[dict[str, Any]] = []
        for index, site in enumerate(raw_sites):
            try:
                descriptor = self._parse_site(index, site, payload.get("spider"))
            except SubscriptionParseError as exc:
                errors.append(f"site[{index}]: {exc}")
                unresolved.append({"index": index, "reason": "invalid_descriptor"})
                continue
            plugins.append(descriptor)

        return SubscriptionSnapshot(
            source_id=source_id,
            site_count=len(raw_sites),
            plugins=tuple(plugins),
            errors=tuple(errors),
            unresolved_sites=tuple(unresolved),
        )

    def _parse_site(
        self, index: int, site: Any, default_jar: Any = None
    ) -> PluginDescriptor:
        if not isinstance(site, dict):
            raise SubscriptionParseError("site must be an object")

        ext_value = site.get("ext")
        ext = decode_ext(ext_value) if ext_value is not None else {}
        api = str(site.get("api") or "").strip()
        source = str(ext.get("source") or "").strip()
        loader = str(ext.get("loader") or "").strip()
        raw = ext.get("raw") is True
        if loader:
            _validate_loader(loader, self._at_base_url)
        if source and not source.startswith(("http://", "https://")):
            raise SubscriptionParseError("plugin source must be an absolute http(s) URL")
        backend_api = str(ext.get("api") or self._at_base_url).strip().rstrip("/")
        _validate_http_url(backend_api, label="AT backend api")
        if not _same_origin(backend_api, self._at_base_url):
            raise SubscriptionParseError("AT backend api is outside the configured AT origin")

        name = str(site.get("name") or site.get("key") or f"site-{index}")
        plugin_id = str(site.get("key") or f"site-{index}")
        filters = ext.get("filters", [])
        if not isinstance(filters, list):
            raise SubscriptionParseError("plugin filters must be a list")
        filter_entries = tuple(
            item for item in filters if isinstance(item, dict)
        )
        native_client_site: dict[str, Any] = {}
        if api == "csp_Push":
            jar = site.get("jar") or default_jar
            if isinstance(jar, str) and jar:
                # FongMi routes external push actions by this fixed key.
                native_client_site = {
                    **site,
                    "key": "push_agent",
                    "type": 3,
                    "api": "csp_Push",
                    "jar": jar,
                }
        return PluginDescriptor(
            plugin_id=plugin_id,
            name=name,
            plugin_type=_classify_plugin(api, source, raw, loader),
            api=api,
            source=source,
            loader=loader,
            raw=raw,
            token=str(ext.get("token") or ""),
            secret=str(ext.get("secret") or ""),
            local_proxy_config=ext.get("local_proxy_config"),
            data=ext.get("data"),
            filters=filter_entries,
            backend_api=backend_api,
            playback_token=str(ext.get("playbackToken") or ""),
            playback_config_url=str(ext.get("playbackConfigUrl") or ""),
            uid=str(ext.get("uid") or ""),
            home_page=str(site.get("homePage") or "").strip(),
            site_options={
                key: site[key]
                for key in (
                    "searchable", "quickSearch", "filterable", "categories",
                    "style", "timeout", "playerType", "indexs",
                )
                if key in site
            },
            native_client_site=native_client_site,
        )


def build_subscription_url(source: SourceConfig) -> str:
    if source.subscription_url:
        _validate_http_url(source.subscription_url, label="subscription_url")
        return source.subscription_url
    if not source.at_base_url or not source.subscription_id:
        raise SubscriptionParseError("source is missing AT subscription settings")
    base = source.at_base_url.rstrip("/")
    subscription_id = quote(source.subscription_id, safe="")
    if source.subscription_token:
        token = quote(source.subscription_token, safe="")
        return f"{base}/sub/{token}/{subscription_id}"
    return f"{base}/sub/{subscription_id}"


class AtSubscriptionClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def fetch(self, source: SourceConfig) -> dict[str, Any]:
        url = build_subscription_url(source)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                raise SubscriptionParseError(
                    "AT subscription request failed before receiving a response"
                ) from exc
        if response.status_code != 200:
            raise SubscriptionParseError(
                f"AT subscription request failed with status {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SubscriptionParseError("AT subscription response is not JSON") from exc
        if not isinstance(payload, dict):
            raise SubscriptionParseError("AT subscription response must be an object")
        return payload


SubscriptionFetcher = Callable[[SourceConfig], Awaitable[dict[str, Any]]]


async def resolve_subscription(
    source: SourceConfig,
    fetcher: SubscriptionFetcher | None = None,
) -> SubscriptionSnapshot:
    payload = await (fetcher or AtSubscriptionClient().fetch)(source)
    subscription_url = build_subscription_url(source)
    at_base_url = source.at_base_url or _origin_url(subscription_url)
    return SubscriptionParser(at_base_url).parse(source.source_id, payload)


class CachedSubscriptionResolver:
    """Share short-lived subscription snapshots across API and T4 requests."""

    def __init__(
        self,
        fetcher: SubscriptionFetcher | None = None,
        *,
        ttl: float = 30.0,
    ) -> None:
        self._fetcher = fetcher
        self._ttl = max(0.0, ttl)
        self._cache: dict[str, tuple[float, SubscriptionSnapshot]] = {}
        self._inflight: dict[str, asyncio.Task[SubscriptionSnapshot]] = {}

    async def resolve(self, source: SourceConfig) -> SubscriptionSnapshot:
        now = time.monotonic()
        cached = self._cache.get(source.source_id)
        if cached is not None and cached[0] > now:
            return cached[1]

        task = self._inflight.get(source.source_id)
        if task is None:
            task = asyncio.create_task(
                self._fetch(source)
            )
            self._inflight[source.source_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._inflight.get(source.source_id) is task:
                self._inflight.pop(source.source_id, None)

    async def _fetch(self, source: SourceConfig) -> SubscriptionSnapshot:
        snapshot = await resolve_subscription(source, self._fetcher)
        # An invalidated in-flight request must not overwrite a forced refresh.
        if self._inflight.get(source.source_id) is asyncio.current_task() and self._ttl > 0:
            self._cache[source.source_id] = (time.monotonic() + self._ttl, snapshot)
        return snapshot

    async def refresh(self, source: SourceConfig) -> SubscriptionSnapshot:
        self.invalidate(source.source_id)
        return await self.resolve(source)

    def invalidate(self, source_id: str | None = None) -> None:
        if source_id is None:
            self._cache.clear()
            self._inflight.clear()
        else:
            self._cache.pop(source_id, None)
            self._inflight.pop(source_id, None)
