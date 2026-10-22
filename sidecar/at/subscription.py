from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
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

    def worker_extend(self) -> str:
        payload: dict[str, Any] = {
            "api": self.api,
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
        return {
            "id": self.plugin_id,
            "name": self.name,
            "type": self.plugin_type,
            "api": self.api,
            "raw": self.raw,
            "filters": len(self.filters),
        }


@dataclass(frozen=True, slots=True)
class SubscriptionSnapshot:
    source_id: str
    site_count: int
    plugins: tuple[PluginDescriptor, ...]
    errors: tuple[str, ...] = ()

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
                if plugin.plugin_id == plugin_id or plugin.name == plugin_id:
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
    return "builtin"


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
        for index, site in enumerate(raw_sites):
            try:
                descriptor = self._parse_site(index, site)
            except SubscriptionParseError as exc:
                errors.append(f"site[{index}]: {exc}")
                continue
            if descriptor.plugin_type != "builtin":
                plugins.append(descriptor)

        return SubscriptionSnapshot(
            source_id=source_id,
            site_count=len(raw_sites),
            plugins=tuple(plugins),
            errors=tuple(errors),
        )

    def _parse_site(self, index: int, site: Any) -> PluginDescriptor:
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

        name = str(site.get("name") or site.get("key") or f"site-{index}")
        plugin_id = str(site.get("key") or f"site-{index}")
        filters = ext.get("filters", [])
        if not isinstance(filters, list):
            raise SubscriptionParseError("plugin filters must be a list")
        filter_entries = tuple(
            item for item in filters if isinstance(item, dict)
        )
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
