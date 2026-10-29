from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any
from urllib.parse import urlsplit
import uuid


@dataclass(frozen=True)
class PluginProxyTarget:
    url: str
    runtime_key: str
    trace: str


class PluginProxyRegistry:
    """Routes are tied to a live Worker instance, not an authentication layer."""

    def __init__(self) -> None:
        self._targets: dict[str, PluginProxyTarget] = {}
        self._lock = threading.Lock()

    def register(self, url: str, runtime_key: str, trace: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/proxy":
            raise ValueError("invalid Worker proxy endpoint")
        key = uuid.uuid4().hex
        with self._lock:
            self._targets[key] = PluginProxyTarget(url, runtime_key, trace)
        return key

    def remove(self, key: str) -> None:
        with self._lock:
            self._targets.pop(key, None)

    def get(self, key: str) -> PluginProxyTarget | None:
        with self._lock:
            return self._targets.get(key)

    def resolve(self, url: str) -> str | None:
        parsed = urlsplit(url)
        prefix = "/t4/proxy/"
        if not parsed.path.startswith(prefix):
            internal = parsed._replace(query="", fragment="").geturl()
            with self._lock:
                if any(target.url == internal for target in self._targets.values()):
                    return url
            return None
        key = parsed.path[len(prefix):].strip("/")
        target = self.get(key)
        return target.url + ("?" + parsed.query if parsed.query else "") if target else None


def is_proxy_text(mime: str) -> bool:
    mime = mime.split(";")[0].strip().lower()
    return mime.startswith("text/") or mime in {
        "application/vnd.apple.mpegurl", "application/x-mpegurl",
        "application/json", "application/dash+xml", "application/xml",
    }


def rewrite_proxy_urls(value: Any, internal: str, public: str) -> Any:
    if isinstance(value, str):
        return value.replace(internal, public)
    if isinstance(value, dict):
        return {key: rewrite_proxy_urls(item, internal, public) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_proxy_urls(item, internal, public) for item in value]
    return value


async def rewrite_chunks(chunks, internal: bytes, public: bytes):
    """Replace endpoints even when a URL crosses an upstream chunk boundary."""
    pending = b""
    async for chunk in chunks:
        pending += chunk
        while True:
            index = pending.find(internal)
            if index < 0:
                break
            yield pending[:index] + public
            pending = pending[index + len(internal):]
        size = max(0, len(pending) - len(internal) + 1)
        if size:
            yield pending[:size]
            pending = pending[size:]
    if pending:
        yield pending
