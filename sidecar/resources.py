from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import gc
import sys
import threading
import time
from pathlib import Path


def resource_snapshot() -> dict:
    """Current Linux measurements, never the sum of shared Worker RSS."""
    result = {}
    root = Path("/sys/fs/cgroup")
    try:
        result["memory_bytes"] = int((root / "memory.current").read_text())
        values = dict(line.split() for line in (root / "memory.stat").read_text().splitlines())
        for field in ("anon", "file"):
            result[field + "_bytes"] = int(values[field])
    except (OSError, ValueError, KeyError):
        pass
    try:
        values = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
        result["gateway_rss_bytes"] = int(values["VmRSS"].split()[0]) * 1024
        result["threads"] = int(values["Threads"].strip())
        result["fds"] = len(tuple(Path("/proc/self/fd").iterdir()))
    except (OSError, ValueError, KeyError):
        pass
    return result


@lru_cache(maxsize=1)
def _malloc_trim():
    if sys.platform != "linux":
        return None
    import ctypes
    try:
        trim = ctypes.CDLL(None).malloc_trim
    except (AttributeError, OSError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim


def release_unused_memory() -> dict:
    collected = gc.collect(2)
    trim = _malloc_trim()
    return {
        "collected": collected,
        "trim_supported": trim is not None,
        "trimmed": bool(trim(0)) if trim is not None else False,
    }


class IdleMemory:
    """Collect once after activity settles, without discarding live plugin state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active = 0
        self._last_used = time.monotonic()
        self._dirty = False

    @contextmanager
    def activity(self):
        with self._lock:
            self._active += 1
            self._dirty = True
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
                self._last_used = time.monotonic()

    def reclaim(self, *, idle_seconds: float = 30, cleanup=None) -> dict:
        with self._lock:
            if self._active:
                return {"reclaimed": False, "reason": "active"}
            if time.monotonic() - self._last_used < idle_seconds:
                return {"reclaimed": False, "reason": "recent"}
            count = cleanup() if cleanup is not None else 0
            if not self._dirty and not count:
                return {"reclaimed": False, "reason": "clean"}
            result = release_unused_memory()
            self._dirty = False
            return {"reclaimed": True, "released_entries": count, **result}


class ResourceActivityMiddleware:
    def __init__(self, app, memory: IdleMemory) -> None:
        self.app, self.memory = app, memory

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        root = scope.get("root_path", "").rstrip("/")
        if root and path.startswith(root + "/"):
            path = path[len(root):]
        if (
            scope["type"] != "http" or path in {"/t4/health", "/t4/", "/t4"}
            or path.startswith(("/t4/api/diagnostics/", "/t4/assets/"))
        ):
            await self.app(scope, receive, send)
            return
        # The ASGI call includes streaming bodies and disconnect cleanup.
        with self.memory.activity():
            await self.app(scope, receive, send)
