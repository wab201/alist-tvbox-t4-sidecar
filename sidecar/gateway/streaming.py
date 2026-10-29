from __future__ import annotations

import time

import anyio
from fastapi.responses import StreamingResponse
import httpx

from sidecar.diagnostics import record


class ManagedStreamingResponse(StreamingResponse):
    """Cancel connection setup and reads when the downstream goes away."""

    def __init__(self, *, parent_trace: str, stage: str):
        super().__init__(iter(()))
        self.parent_trace, self.stage = parent_trace, stage
        self.upstream: httpx.Response | None = None
        self.count = 0
        self.reason = "cancelled"

    async def stream_response(self, send):
        iterator = self.body_iterator

        async def tracked():
            async for chunk in iterator:
                yield chunk
            self.reason = "complete"

        self.body_iterator = tracked()
        await super().stream_response(send)

    async def __call__(self, scope, receive, send):
        start = time.monotonic()
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
                    if self.upstream is not None:
                        await self.upstream.aclose()
            finally:
                elapsed = time.monotonic() - start
                record(self.stage + ".closed", parent_trace=self.parent_trace, bytes=self.count,
                       reason=self.reason, duration_ms=round(elapsed * 1000),
                       mib_per_s=round(self.count / (1024 ** 2) / max(elapsed, .001), 3))
