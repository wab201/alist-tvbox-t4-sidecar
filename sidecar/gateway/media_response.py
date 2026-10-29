from __future__ import annotations

import time

from fastapi import HTTPException, Request
import httpx

from sidecar.diagnostics import record
from sidecar.gateway.streaming import ManagedStreamingResponse
from sidecar.playback import MediaAsset, PlaybackBridge, PlaybackError


class MediaResponse(ManagedStreamingResponse):
    """Own the upstream from connection setup through downstream disconnect."""

    def __init__(self, bridge: PlaybackBridge, key: str, asset: MediaAsset, request: Request):
        super().__init__(parent_trace=asset.trace, stage="media")
        self.bridge, self.key, self.asset, self.request = bridge, key, asset, request

    async def stream_response(self, send):
        asset, request = self.asset, self.request
        start = time.monotonic()
        try:
            if asset.kind == "hls":
                content = await self.bridge.render_hls_async(self.key, asset, str(request.base_url))
            else:
                content = asset.content
            if content is not None:
                self.init_headers({
                    "Cache-Control": "no-store", "Content-Type": asset.mime,
                    "Content-Length": str(len(content)),
                })

                async def manifest():
                    if request.method != "HEAD":
                        self.count = len(content)
                        yield content

                self.body_iterator = manifest()
                record("media.manifest", parent_trace=asset.trace, bytes=len(content))
            else:
                self.upstream = await self.bridge.open_media_async(
                    asset, request.method, dict(request.headers),
                )
                upstream = self.upstream
                names = ["content-type", "content-length", "content-range", "accept-ranges"]
                if asset.kind != "segment":
                    names += ["etag", "last-modified", "content-encoding"]
                headers = {name: upstream.headers[name] for name in names if name in upstream.headers}
                headers["Cache-Control"] = "no-store"
                headers["X-Accel-Buffering"] = "no"
                headers.setdefault("content-type", asset.mime)
                self.init_headers(headers)
                self.status_code = upstream.status_code
                record("media.upstream", parent_trace=asset.trace, status=upstream.status_code,
                       range=bool(request.headers.get("range")),
                       duration_ms=round((time.monotonic() - start) * 1000))

                async def chunks():
                    if request.method != "HEAD":
                        async for chunk in upstream.aiter_raw():
                            self.count += len(chunk)
                            yield chunk

                self.body_iterator = chunks()
            try:
                await super().stream_response(send)
            except httpx.HTTPError as exc:
                self.reason = "upstream_error"
                record("media.stream_failed", parent_trace=asset.trace, error=type(exc).__name__)
                raise
            self.reason = "complete"
        except (PlaybackError, httpx.HTTPError) as exc:
            if self.reason == "upstream_error":
                raise  # Headers are already sent; a truncated stream must remain a failure.
            self.reason = "upstream_error"
            if asset.kind == "hls":
                record("media.hls_failed", parent_trace=asset.trace, error=type(exc).__name__)
                raise HTTPException(502, detail={
                    "code": "hls_delivery_failed", "message": "服务端 HLS 清单处理失败",
                }) from exc
            record("media.failed", parent_trace=asset.trace, error=type(exc).__name__)
            raise HTTPException(502, "媒体上游连接失败，请查看链路日志") from exc
