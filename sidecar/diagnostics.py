from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from logging.handlers import RotatingFileHandler
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any
import uuid


_context: ContextVar[dict[str, Any]] = ContextVar("t4_trace", default={})
_FIELDS = {
    "source", "plugin", "module", "backend", "route", "method", "error",
    "status", "duration_ms", "count", "bytes", "scheme", "format",
    "header_count", "parse", "jx", "parent_trace", "asset", "range",
    "level", "fingerprint", "session", "reason", "video", "audio",
    "root_error", "missing_module", "error_file", "error_function", "error_line",
    "errno", "winerror",
    "mib_per_s",
}
_LABEL = re.compile(r"^[\w.:-]{1,100}$", re.UNICODE)
_MIMES = {
    "application/dash+xml", "application/octet-stream", "application/x-mpegurl",
    "application/vnd.apple.mpegurl", "video/mp4", "audio/mp4", "video/mp2t",
    "audio/mpeg", "text/html",
}


def safe_label(value: Any) -> str:
    text = str(value)
    if _LABEL.fullmatch(text) and not text.startswith(("http:", "https:", "data:")):
        return text
    return "hash:" + hashlib.sha256(text.encode()).hexdigest()[:16]


class TraceStore:
    """Only typed metadata is retained. Payloads, URLs and headers are never logged."""

    def __init__(self, path: str | None = None, capacity: int = 2000) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._sequence = 0
        self._handler = None
        if path:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            for previous in (Path(str(target) + ".2"), Path(str(target) + ".1"), target):
                if previous.is_file():
                    with previous.open(encoding="utf-8") as stream:
                        for line in stream:
                            try:
                                item = json.loads(line)
                            except ValueError:
                                continue
                            if (
                                isinstance(item, dict)
                                and isinstance(item.get("seq"), int)
                                and isinstance(item.get("time"), (int, float))
                                and isinstance(item.get("stage"), str)
                                and isinstance(item.get("trace_id"), str)
                            ):
                                allowed = _FIELDS | {"seq", "time", "stage", "trace_id"}
                                self._events.append({
                                    key: value for key, value in item.items()
                                    if key in allowed and isinstance(value, (str, int, float, bool))
                                })
                                self._sequence = max(self._sequence, item["seq"])
            self._handler = RotatingFileHandler(
                target, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
            )
            self._handler.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, stage: str, *, trace_id: str = "", **fields: Any) -> None:
        values = {
            key: value if isinstance(value, (int, float, bool))
            or (key == "format" and isinstance(value, str) and value in _MIMES)
            else safe_label(value)
            for key, value in fields.items() if key in _FIELDS and value is not None
        }
        with self._lock:
            self._sequence += 1
            event = {
                "seq": self._sequence,
                "time": round(time.time(), 3),
                "trace_id": safe_label(trace_id) if trace_id else "",
                "stage": safe_label(stage),
                **values,
            }
            self._events.append(event)
            if self._handler is not None:
                self._handler.emit(logging.LogRecord(
                    "t4.trace", logging.INFO, "", 0,
                    json.dumps(event, ensure_ascii=True), (), None,
                ))

    def events(
        self, *, trace_id: str = "", after: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self._lock:
            related = {trace_id} if trace_id else set()
            if trace_id:
                previous = set()
                while previous != related:
                    previous = set(related)
                    for item in self._events:
                        parent = item.get("parent_trace")
                        if parent and (item["trace_id"] in related or parent in related):
                            related.update((item["trace_id"], parent))
            return [
                dict(item) for item in self._events
                if item["seq"] > after and (
                    not trace_id or item["trace_id"] in related
                )
            ][-max(1, min(limit, 2000)):]

    def close(self) -> None:
        if self._handler is not None:
            self._handler.close()


@contextmanager
def trace_scope(store: TraceStore | None = None, **fields: Any):
    current = dict(_context.get())
    if store is not None:
        current["store"] = store
    current.update(fields)
    token = _context.set(current)
    try:
        yield
    finally:
        _context.reset(token)


def current_trace() -> dict[str, Any]:
    return dict(_context.get())


def trace_id() -> str:
    return str(_context.get().get("trace_id", ""))


def new_trace_id() -> str:
    return uuid.uuid4().hex


def record(stage: str, **fields: Any) -> None:
    context = current_trace()
    store = context.pop("store", None)
    if store is not None:
        context.update(fields)
        store.emit(stage, **context)


class TraceMiddleware:
    def __init__(self, app: Any, store: TraceStore) -> None:
        self.app = app
        self.store = store

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        identifier = new_trace_id()
        start = time.monotonic()
        status = 500
        path = scope.get("path", "")
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and (path == root_path or path.startswith(root_path + "/")):
            path = path[len(root_path):] or "/"
        route = next((name for prefix, name in (
            ("/t4/fm/", "fm"), ("/t4/media/", "media"),
            ("/t4/images/", "image"),
            ("/t4/proxy/", "proxy"),
            ("/t4/api/diagnostics/", "diagnostics"),
            ("/t4/config.json", "subscription"),
            ("/t4/api/", "api"),
        ) if path.startswith(prefix)), "web")

        async def traced_send(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append(
                    (b"x-t4-trace-id", identifier.encode("ascii"))
                )
            await send(message)

        with trace_scope(self.store, trace_id=identifier):
            try:
                await self.app(scope, receive, traced_send)
            except Exception as exc:
                record("request.failed", error=type(exc).__name__, route=route)
                raise
            finally:
                if route not in {"web", "diagnostics"} or status >= 400:
                    record("request.complete", route=route, method=scope["method"],
                           status=status, duration_ms=round((time.monotonic() - start) * 1000))
