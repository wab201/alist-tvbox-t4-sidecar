from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import json
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx

from sidecar.diagnostics import record, trace_id
from sidecar.image_headers import ImageHeaderRule, match_image_headers


def split_image(value: str) -> tuple[str, dict[str, str]]:
    """Only explicit header syntax is metadata; CDN @resize remains a URL."""
    if "@{" in value:
        url, raw = value.rsplit("@{", 1)
        raw = "{" + raw
        headers = json.loads(raw)
        if not isinstance(headers, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError("invalid image headers")
        return url, headers
    match = re.search(r"@(?=[A-Za-z][A-Za-z0-9-]*=)", value)
    if match:
        headers = {}
        for part in value[match.start() + 1:].split("@"):
            key, separator, val = part.partition("=")
            if not separator:
                raise ValueError("invalid image headers")
            if key == "Headers":
                decoded = json.loads(val)
                if not isinstance(decoded, dict) or any(
                    not isinstance(k, str) or not isinstance(v, str) for k, v in decoded.items()
                ):
                    raise ValueError("invalid image headers")
                headers.update(decoded)
            else:
                headers[key] = val
        return value[:match.start()], headers
    return value, {}


def image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    if data.startswith(b"BM"):
        return "image/bmp"
    return ""


@dataclass
class ImageAsset:
    url: str
    headers: dict[str, str]
    trace: str
    touched: float
    inline_headers: dict[str, str] = field(default_factory=dict)
    rules: tuple[ImageHeaderRule, ...] = field(default=(), repr=False)
    content: bytes | None = None
    mime: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ImageBridge:
    """Lazy, byte-bounded cover delivery; listing never downloads posters."""

    def __init__(self, *, transport=None, capacity=1024, cache_bytes=8 * 1024 * 1024,
                 max_bytes=2 * 1024 * 1024, ttl=7200):
        self.client = httpx.AsyncClient(
            transport=transport, follow_redirects=True, timeout=httpx.Timeout(12, connect=4),
        )
        self._assets: OrderedDict[str, ImageAsset] = OrderedDict()
        self._capacity, self._cache_bytes, self._max_bytes, self._ttl = capacity, cache_bytes, max_bytes, ttl
        self._bytes = 0
        self._slots = asyncio.Semaphore(4)

    def prepare(self, value: str, base: str, backend: str, *,
                rules: tuple[ImageHeaderRule, ...] = ()) -> str:
        if not value or not base:
            return value
        try:
            url, headers = split_image(value)
        except (ValueError, TypeError):
            record("image.invalid", reason="header_syntax")
            return value
        relative = not urlsplit(url).scheme
        if relative:
            # AT emits service-root image paths even behind a reverse-proxy prefix.
            url = (
                backend.rstrip("/") + "/" + url.lstrip("/")
                if url.startswith(("/images", "/proxy-image"))
                else urljoin(backend.rstrip("/") + "/", url)
            )
        target = urlsplit(url)
        if target.scheme not in {"http", "https"} or not target.hostname:
            return value
        inline_headers = {k.lower(): v.strip(" \t") for k, v in headers.items()}
        headers = self._headers(url, inline_headers, rules)
        backend_url = urlsplit(backend)
        at_prefix = backend_url.path.rstrip("/") + "/"
        at_path = target.path[len(at_prefix) - 1:] if target.path.startswith(at_prefix) else target.path
        at_image = target.netloc == backend_url.netloc and at_path.startswith(("/images", "/proxy-image"))
        if not (headers or relative or at_image):
            return value
        if any("\r" in k + v or "\n" in k + v for k, v in headers.items()):
            return value
        self.prune_expired()
        # Redirects can match a different rule; include the entire rule context.
        context = [[rule.host, rule.headers] for rule in rules]
        key = hashlib.sha256(json.dumps(
            [url, headers, inline_headers, context], sort_keys=True,
        ).encode()).hexdigest()[:32]
        if key not in self._assets:
            self._assets[key] = ImageAsset(
                url, headers, trace_id(), time.monotonic(), inline_headers, rules,
            )
        self._assets[key].touched = time.monotonic()
        self._assets.move_to_end(key)
        while len(self._assets) > self._capacity:
            _, removed = self._assets.popitem(last=False)
            self._bytes -= len(removed.content or b"")
        return base.rstrip("/") + "/t4/images/" + key

    @staticmethod
    def _headers(url: str, inline: dict[str, str],
                 rules: tuple[ImageHeaderRule, ...]) -> dict[str, str]:
        host = urlsplit(url).hostname or ""
        headers = {}
        if host.endswith(".doubanio.com"):
            headers = {"referer": "https://movie.douban.com/", "user-agent": "Mozilla/5.0"}
        elif host.endswith(".hdslb.com") or host == "hdslb.com":
            headers = {"referer": "https://www.bilibili.com/", "user-agent": "Mozilla/5.0"}
        # FongMi's network interceptor applies subscription rules after inline headers.
        return {**headers, **inline, **match_image_headers(host, rules)}

    @asynccontextmanager
    async def _response(self, asset: ImageAsset):
        url = asset.url
        for redirects in range(21):
            inline = asset.inline_headers
            if urlsplit(url).netloc != urlsplit(asset.url).netloc:
                inline = {k: v for k, v in inline.items() if k not in {"cookie", "authorization"}}
            headers = self._headers(url, inline, asset.rules)
            # A raw Request avoids sharing response cookies across subscriptions.
            request = httpx.Request("GET", url, headers={**self.client.headers, **headers})
            response = await self.client.send(request, stream=True, follow_redirects=False)
            try:
                if response.has_redirect_location:
                    if redirects == 20:
                        raise httpx.TooManyRedirects("too many image redirects", request=request)
                    record("image.redirect", status=response.status_code, parent_trace=asset.trace)
                    url = str(response.next_request.url)
                    continue
                yield response
                return
            finally:
                await response.aclose()

    def prune_expired(self) -> int:
        expired = []
        cutoff = time.monotonic() - self._ttl
        # Every touch moves to the end, so the first fresh entry ends the scan.
        for key, asset in self._assets.items():
            if asset.touched >= cutoff:
                break
            if not asset.lock.locked():
                expired.append(key)
        for key in expired:
            asset = self._assets.pop(key)
            self._bytes -= len(asset.content or b"")
        return len(expired)

    async def fetch(self, key: str) -> tuple[bytes, str]:
        self.prune_expired()
        asset = self._assets.get(key)
        if asset is None:
            raise KeyError(key)
        asset.touched = time.monotonic()
        self._assets.move_to_end(key)
        async with asset.lock:
            if asset.content is not None:
                return asset.content, asset.mime
            async with self._slots:
                for attempt in range(2):
                    async with self._response(asset) as response:
                        record("image.upstream", status=response.status_code, parent_trace=asset.trace)
                        if 500 <= response.status_code < 600 and attempt == 0:
                            record("image.retry", status=response.status_code, parent_trace=asset.trace)
                            continue
                        response.raise_for_status()
                        content = bytearray()
                        async for chunk in response.aiter_bytes(16384):
                            if len(content) + len(chunk) > self._max_bytes:
                                raise ValueError("image_too_large")
                            content.extend(chunk)
                        mime = image_mime(content)
                        if not mime:
                            raise ValueError("not_image_bytes")
                        break
            data = bytes(content)
            # Entries removed during I/O must not be counted as retained cache.
            if self._assets.get(key) is asset and len(data) <= self._cache_bytes:
                for other in self._assets.values():
                    if self._bytes + len(data) <= self._cache_bytes:
                        break
                    self._bytes -= len(other.content or b"")
                    other.content = None
                asset.content, asset.mime = data, mime
                self._bytes += len(data)
            return data, mime

    async def aclose(self):
        await self.client.aclose()
        self._assets.clear()
        self._bytes = 0
