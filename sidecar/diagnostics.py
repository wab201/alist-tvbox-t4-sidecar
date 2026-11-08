from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from logging.handlers import RotatingFileHandler
import logging
import math
from pathlib import Path
import queue
import re
import sys
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
    "memory_bytes", "anon_bytes", "file_bytes", "gateway_rss_bytes", "threads", "fds",
    "workers", "retained_workers", "proxy_workers", "busy_workers", "gate_workers",
    "interval_seconds", "version",
    "profile", "client",
    "engine", "first_body_ms", "upstream_headers_ms", "control_ms",
    "selected_engine", "previous_engine", "engine_generation", "route_reason",
    "response_ready_ms", "first_body_size", "chunks", "max_chunk_size",
    "body_read_wait_ms", "max_body_read_wait_ms", "downstream_wait_ms", "max_downstream_wait_ms",
    "downstream_poll_gap_ms", "max_downstream_poll_gap_ms", "active_at_start",
    "range_kind", "range_start", "range_end", "range_suffix", "expected_bytes",
    "response_range_start", "response_range_end", "resource_bytes", "active", "peak",
    "cpu_usage_us", "cpu_throttled_us", "cpu_throttled_periods", "gateway_cpu_ms",
    "loop_lag_ms", "loop_lag_max_ms", "loop_samples",
    "log_dropped", "log_pending", "log_write_errors", "read_errors",
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


def range_metadata(value: str) -> dict:
    """Retain numeric byte ranges only; never retain arbitrary header text."""
    if not value:
        return {"range_kind": "none"}
    match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", value.strip())
    if not match or not any(match.groups()):
        return {"range_kind": "other"}
    start, end = match.groups()
    values = {"range_kind": "bounded" if start and end else "open" if start else "suffix"}
    if start:
        values["range_start"] = int(start)
    if end:
        values["range_end" if start else "range_suffix"] = int(end)
    return values


def response_metadata(headers) -> dict:
    values = {}
    length = headers.get("content-length", "")
    if re.fullmatch(r"\d{1,20}", length):
        values["expected_bytes"] = int(length)
    match = re.fullmatch(r"bytes (\d{1,20})-(\d{1,20})/(\d{1,20}|\*)", headers.get("content-range", ""))
    if match:
        values.update(response_range_start=int(match[1]), response_range_end=int(match[2]))
        if match[3] != "*":
            values["resource_bytes"] = int(match[3])
    return values


def media_summary(events: list[dict]) -> list[dict]:
    """Descriptive retained-window statistics, not an unmatched speedup estimate."""
    groups = {}
    for event in events:
        if event.get("stage") != "media.closed" or event.get("schema") != 2:
            continue
        key = tuple(event.get(name) for name in ("run_id", "engine", "selected_engine", "engine_generation"))
        groups.setdefault(key, []).append(event)
    result = []
    for key, rows in groups.items():
        completed = [row for row in rows if row.get("reason") == "complete" and row.get("status", 500) < 400]
        durations = {}
        for field in ("first_body_ms", "control_ms", "upstream_headers_ms", "body_read_wait_ms",
                      "downstream_wait_ms", "downstream_poll_gap_ms", "duration_ms"):
            values = sorted(row[field] for row in completed if isinstance(row.get(field), (int, float)))
            if values:
                durations[field] = {"count": len(values), "p50": values[math.ceil(len(values) * .5) - 1],
                                    "p95": values[math.ceil(len(values) * .95) - 1], "max": values[-1]}
        result.append({**dict(zip(("run_id", "engine", "selected_engine", "engine_generation"), key)),
                       "requests": len(rows), "completed": len(completed), "failed_or_cancelled": len(rows) - len(completed),
                       "bytes": sum(row.get("bytes", 0) for row in rows), "timings": durations})
    return result


class TraceStore:
    """Only typed metadata is retained. Payloads, URLs and headers are never logged."""

    def __init__(
        self, path: str | None = None, capacity: int = 2000, *, console: bool = True,
        async_output: bool = False, version: str = "",
    ) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self.run_id = uuid.uuid4().hex
        self.version = version
        self._queue = queue.Queue(maxsize=1024) if async_output else None
        self._stop = threading.Event()
        self._writer = None
        self._closed = False
        self._written = self._dropped = self._write_errors = self._high_water = 0
        self._media = {engine: dict(active=0, peak=0, started=0, completed=0, errors=0,
                                   cancelled=0, bytes=0) for engine in ("python", "rust")}
        self._sequence = 0
        self._handler = None
        # stderr is collected by Docker and leaves CLI stdout valid JSON.
        self._console = logging.StreamHandler(sys.stderr) if console else None
        self._console_window = time.monotonic()
        self._console_count = 0
        self._console_suppressed = 0
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
                                allowed = _FIELDS | {"seq", "time", "stage", "trace_id", "schema", "run_id"}
                                self._events.append({
                                    key: value for key, value in item.items()
                                    if key in allowed and isinstance(value, (str, int, float, bool))
                                })
                                self._sequence = max(self._sequence, item["seq"])
            self._handler = RotatingFileHandler(
                target, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
            )
            self._handler.setFormatter(logging.Formatter("%(message)s"))
            self._handler.handleError = self._output_error
        if self._console is not None:
            self._console.handleError = self._output_error
        if self._queue is not None:
            self._writer = threading.Thread(target=self._write_loop, name="t4-log-writer", daemon=True)
            self._writer.start()

    def start_output(self) -> None:
        with self._lock:
            if self._writer is None and not self._closed:
                self._queue = queue.Queue(maxsize=1024)
                self._writer = threading.Thread(target=self._write_loop, name="t4-log-writer", daemon=True)
                self._writer.start()

    def _output_error(self, _record) -> None:
        self._write_errors += 1

    def emit(self, stage: str, *, trace_id: str = "", **fields: Any) -> None:
        values = {
            key: value if isinstance(value, (int, float, bool))
            or (key == "format" and isinstance(value, str) and value in _MIMES)
            else safe_label(value)
            for key, value in fields.items() if key in _FIELDS and value is not None
            and not (isinstance(value, float) and not math.isfinite(value))
        }
        with self._lock:
            if self._closed:
                return
            self._sequence += 1
            event = {
                "seq": self._sequence,
                "time": round(time.time(), 3),
                "trace_id": safe_label(trace_id) if trace_id else "",
                "stage": safe_label(stage),
                "schema": 2, "run_id": self.run_id, "version": self.version,
                **values,
            }
            self._events.append(event)
            counters = self._media.get(event.get("engine"))
            if counters is not None and stage == "media.started":
                counters["active"] += 1
                counters["started"] += 1
                counters["peak"] = max(counters["peak"], counters["active"])
            elif counters is not None and stage == "media.closed":
                counters["active"] = max(0, counters["active"] - 1)
                reason = event.get("reason")
                counters["completed" if reason == "complete" else "errors" if reason == "upstream_error" else "cancelled"] += 1
                counters["bytes"] += event.get("bytes", 0)
            if self._queue is not None:
                try:
                    self._queue.put_nowait(event)
                    self._high_water = max(self._high_water, self._queue.qsize())
                except queue.Full:
                    self._dropped += 1
            else:
                self._write_output(event)

    def _write_output(self, event: dict) -> None:
        try:
            if self._handler is not None:
                self._handler.emit(logging.LogRecord(
                    "t4.trace", logging.INFO, "", 0,
                    json.dumps(event, ensure_ascii=True), (), None,
                ))
            stage = event["stage"]
            if self._console is not None and stage not in {
                "adapter.start", "worker.start", "backend.invoke", "id.resolved",
            }:
                now = time.monotonic()
                if now - self._console_window >= 10:
                    if self._console_suppressed:
                        self._write_console({
                            "time": event["time"], "stage": "log.suppressed",
                            "count": self._console_suppressed, "interval_seconds": 10,
                        })
                    self._console_window = now
                    self._console_count = self._console_suppressed = 0
                # Keep heartbeat/reclamation visible even during a request flood.
                priority = stage.startswith(("runtime.", "gateway.memory", "worker.retired"))
                if priority or self._console_count < 100:
                    self._write_console(event)
                    self._console_count += 1
                else:
                    self._console_suppressed += 1
            self._written += 1
        except Exception:
            # A failing log device must not block media or recursively log errors.
            self._write_errors += 1

    def _write_loop(self) -> None:
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    event = self._queue.get(timeout=.2)
                except queue.Empty:
                    continue
                try:
                    self._write_output(event)
                finally:
                    self._queue.task_done()
        finally:
            self._close_outputs()

    def output_status(self) -> dict:
        return {"async": self._queue is not None, "capacity": 1024 if self._queue is not None else 0,
                "pending": self._queue.qsize() if self._queue is not None else 0,
                "high_water": self._high_water, "written": self._written, "dropped": self._dropped,
                "write_errors": self._write_errors, "console_suppressed": self._console_suppressed,
                "retained": len(self._events), "retention_capacity": self._events.maxlen,
                "run_id": self.run_id}

    def media_status(self) -> dict:
        with self._lock:
            return {key: dict(value) for key, value in self._media.items()}

    def _write_console(self, event: dict) -> None:
        self._console.emit(logging.LogRecord(
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
        with self._lock:
            self._closed = True
        if self._writer is not None:
            self._stop.set()
            self._writer.join(timeout=3)
        else:
            self._close_outputs()

    def _close_outputs(self) -> None:
        if self._handler is not None:
            self._handler.close()
        if self._console is not None:
            self._console.close()


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
    def __init__(self, app: Any, store: TraceStore, media_engine=None) -> None:
        self.app = app
        self.store = store
        self.media_engine = media_engine

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        identifier = new_trace_id()
        context = {}
        if self.media_engine is not None:
            choice = self.media_engine.snapshot()
            context = {"selected_engine": choice["selected_engine"], "engine_generation": choice["generation"]}
            # Only the private loopback server accepts relay metadata. The public
            # Rust ingress strips client-supplied x-t4-relay-* before adding these.
            # ProxyHeadersMiddleware may already replace scope.client with the
            # real player IP; availability is set only by the loopback launcher.
            if self.media_engine.available:
                headers = dict(scope.get("headers", []))
                value = headers.get(b"x-t4-relay-trace", b"").decode("ascii", "ignore")
                if re.fullmatch(r"[a-f0-9]{32}", value):
                    identifier = value
                    for name, field in (("selected", "selected_engine"), ("reason", "route_reason")):
                        value = headers.get(f"x-t4-relay-{name}".encode(), b"").decode("ascii", "ignore")
                        if _LABEL.fullmatch(value):
                            context[field] = value
                    for name, field in (("generation", "engine_generation"), ("control-ms", "control_ms")):
                        try:
                            value = float(headers.get(f"x-t4-relay-{name}".encode(), b""))
                            if math.isfinite(value) and value >= 0:
                                context[field] = int(value) if field == "engine_generation" else value
                        except ValueError:
                            pass
        start = time.monotonic()
        status = 500
        path = scope.get("path", "")
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and (path == root_path or path.startswith(root_path + "/")):
            path = path[len(root_path):] or "/"
        route = next((name for prefix, name in (
            ("/t4/fm/", "fm"), ("/t4/media/", "media"),
            ("/t4/profiles/", "profile"),
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

        with trace_scope(self.store, trace_id=identifier, **context):
            try:
                await self.app(scope, receive, traced_send)
            except Exception as exc:
                record("request.failed", error=type(exc).__name__, route=route)
                raise
            finally:
                if route not in {"web", "diagnostics"} or status >= 400:
                    record("request.complete", route=route, method=scope["method"],
                           status=status, duration_ms=round((time.monotonic() - start) * 1000))
