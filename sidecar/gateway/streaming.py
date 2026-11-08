from __future__ import annotations

import time

import anyio
from fastapi.responses import StreamingResponse
import httpx

from sidecar.diagnostics import record, current_trace, range_metadata, response_metadata


class ManagedStreamingResponse(StreamingResponse):
    """Cancel connection setup and reads when the downstream goes away."""

    def __init__(self, *, parent_trace: str, stage: str):
        super().__init__(iter(()))
        self.parent_trace, self.stage = parent_trace, stage
        self.upstream: httpx.Response | None = None
        self.count = 0
        self.reason = "cancelled"
        self.first_body_ms = None
        self.started = 0.0
        self.control_ms = 0.0
        self.upstream_headers_ms = None
        self.response_ready_ms = None
        self.chunks = self.max_chunk_size = 0
        self.first_body_size = None
        self.body_read_wait_ms = self.max_body_read_wait_ms = 0.0
        self.downstream_wait_ms = self.max_downstream_wait_ms = 0.0
        self._request_fields = {}
        self._progress_at = 0.0

    def elapsed_ms(self):
        return round((time.monotonic() - self.started) * 1000 + self.control_ms, 3)

    def metrics(self):
        return {
            **self._request_fields, **response_metadata(self.headers),
            "parent_trace": self.parent_trace, "engine": "python", "bytes": self.count,
            "first_body_ms": self.first_body_ms, "upstream_headers_ms": self.upstream_headers_ms,
            "response_ready_ms": self.response_ready_ms, "control_ms": self.control_ms,
            "chunks": self.chunks, "first_body_size": self.first_body_size,
            "max_chunk_size": self.max_chunk_size,
            "body_read_wait_ms": round(self.body_read_wait_ms, 3),
            "max_body_read_wait_ms": round(self.max_body_read_wait_ms, 3),
            "downstream_wait_ms": round(self.downstream_wait_ms, 3),
            "max_downstream_wait_ms": round(self.max_downstream_wait_ms, 3),
            "status": self.status_code, "duration_ms": self.elapsed_ms(),
        }

    async def stream_response(self, send):
        iterator = self.body_iterator
        self.response_ready_ms = self.elapsed_ms()

        async def tracked():
            try:
                while True:
                    started = time.monotonic()
                    try:
                        chunk = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    finally:
                        waited = (time.monotonic() - started) * 1000
                        self.body_read_wait_ms += waited
                        self.max_body_read_wait_ms = max(self.max_body_read_wait_ms, waited)
                    if chunk and self.first_body_ms is None:
                        self.first_body_ms = self.elapsed_ms()
                        self.first_body_size = len(chunk)
                    if chunk:
                        self.chunks += 1
                        self.max_chunk_size = max(self.max_chunk_size, len(chunk))
                    yield chunk
                self.reason = "complete"
            finally:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    with anyio.CancelScope(shield=True):
                        await close()

        self.body_iterator = tracked()

        async def timed_send(message):
            started = time.monotonic()
            try:
                await send(message)
            finally:
                if message["type"] == "http.response.body" and message.get("body"):
                    waited = (time.monotonic() - started) * 1000
                    self.downstream_wait_ms += waited
                    self.max_downstream_wait_ms = max(self.max_downstream_wait_ms, waited)
            now = time.monotonic()
            if now - self._progress_at >= 10:
                record(self.stage + ".progress", **self.metrics())
                self._progress_at = now

        await super().stream_response(timed_send)

    async def __call__(self, scope, receive, send):
        start = time.monotonic()
        self.started = start
        self._progress_at = start
        self.control_ms = current_trace().get("control_ms", 0.0)
        headers = dict(scope.get("headers", []))
        self._request_fields = {
            "method": scope["method"], "range": b"range" in headers,
            **range_metadata(headers.get(b"range", b"").decode("ascii", "ignore")),
        }
        if hasattr(self, "key"):
            self._request_fields["asset"] = self.key
        store = current_trace().get("store")
        if store is not None:
            self._request_fields["active_at_start"] = store.media_status()["python"]["active"] + 1
        record(self.stage + ".started", engine="python", parent_trace=self.parent_trace, **self._request_fields)
        failure = None
        try:
            # ASGI 2.4+ also needs a listener to cancel stalled reads before a send.
            async with anyio.create_task_group() as tasks:
                async def disconnected():
                    await self.listen_for_disconnect(receive)
                    if self.reason != "complete":
                        self.reason = "client_disconnect"
                        record(self.stage + ".disconnected", parent_trace=self.parent_trace, bytes=self.count)
                    tasks.cancel_scope.cancel()

                tasks.start_soon(disconnected)
                try:
                    await self.stream_response(send)
                except OSError:
                    self.reason = "client_disconnect"
                except Exception as exc:
                    failure = exc
                finally:
                    tasks.cancel_scope.cancel()
            if failure is not None:
                raise failure
        finally:
            # Closing must survive the cancellation used to stop the stream.
            try:
                with anyio.CancelScope(shield=True):
                    try:
                        close = getattr(self.body_iterator, "aclose", None)
                        if close is not None:
                            await close()
                    finally:
                        if self.upstream is not None:
                            await self.upstream.aclose()
            finally:
                elapsed = time.monotonic() - start
                record(self.stage + ".closed", **self.metrics(), reason=self.reason,
                       mib_per_s=round(self.count / (1024 ** 2) / max(elapsed, .001), 3))
