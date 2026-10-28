from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
import httpx
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from sidecar.diagnostics import TraceStore, record, trace_id
from sidecar.playback import PlaybackBridge, PlaybackError
from sidecar.t4.adapter import T4Adapter, T4Request
from sidecar.worker.client import WorkerRemoteError


class PlaybackCheck(BaseModel):
    source_id: str = Field(min_length=1, max_length=100)
    plugin_id: str = Field(min_length=1, max_length=150)
    keyword: str = Field(min_length=1, max_length=120)


def build_diagnostics_router(
    store: TraceStore, bridge: PlaybackBridge, adapter: T4Adapter
) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/diagnostics/events")
    async def events(
        trace: str = "", after: int = 0, limit: int = Query(200, ge=1, le=2000)
    ) -> dict[str, Any]:
        return {"events": store.events(trace_id=trace, after=after, limit=limit)}

    @router.get("/t4/api/diagnostics/export")
    async def export(trace: str = "") -> Response:
        lines = "".join(
            json.dumps(item, ensure_ascii=False) + "\n"
            for item in store.events(trace_id=trace, limit=2000)
        )
        return Response(lines, media_type="application/x-ndjson", headers={
            "Content-Disposition": 'attachment; filename="t4-playback-log.jsonl"',
            "Cache-Control": "no-store",
        })

    @router.post("/t4/api/diagnostics/check")
    async def check(body: PlaybackCheck, request: Request) -> dict[str, Any]:
        async def call(module: str, params: dict[str, Any]) -> dict[str, Any]:
            return await adapter.dispatch(T4Request(
                module, body.source_id, body.plugin_id, params, str(request.base_url),
            ))
        stage = "search"
        try:
            listing = await call("search", {"wd": body.keyword, "quick": False})
            if not listing.get("list"):
                return {"ok": False, "stage": stage, "reason": "no_results", "trace_id": trace_id()}
            stage = "detail"
            detail = await call("detail", {"ac": "detail", "ids": listing["list"][0]["vod_id"]})
            vod = next(iter(detail.get("list") or []), {})
            groups = str(vod.get("vod_play_url") or "").split("$$$")
            flags = str(vod.get("vod_play_from") or "").split("$$$")
            selected = next((
                (flag, entry.partition("$")[2])
                for flag, group in zip(flags, groups) for entry in group.split("#")
                if entry.partition("$")[2]
                and flag.casefold() not in {"操作", "互动", "关注", "未开播", "已下播", "actions", "follow"}
                and not entry.partition("$")[0].startswith((
                    "检查", "刷新", "删除", "清空", "点赞", "投币", "收藏", "取消", "互动状态", "关注",
                ))
            ), None)
            if selected is None:
                return {"ok": False, "stage": stage, "reason": "no_episodes", "trace_id": trace_id()}
            stage = "play"
            payload = await call("play", {"play": selected[1], "flag": selected[0]})
            payload = await asyncio.to_thread(bridge.prepare, payload, str(request.base_url))
            stage = "media"
            result = await asyncio.to_thread(bridge.probe, payload)
            return {**result, "trace_id": trace_id()}
        except Exception as exc:
            details = exc.details if isinstance(exc, WorkerRemoteError) else {}
            record("check.failed", error=type(exc).__name__, reason=stage, **details)
            return {
                "ok": False, "stage": stage, "error": type(exc).__name__,
                "trace_id": trace_id(), **details,
            }

    @router.api_route("/t4/media/{key}", methods=["GET", "HEAD"])
    async def media(key: str, request: Request):
        asset = bridge.get(key)
        if asset is None:
            record("media.expired", status=410)
            raise HTTPException(410, "播放清单已过期，请重新打开详情并播放")
        if asset.kind == "hls":
            try:
                content = await asyncio.to_thread(bridge.render_hls, key, asset, str(request.base_url))
            except (PlaybackError, httpx.HTTPError) as exc:
                record("media.hls_failed", parent_trace=asset.trace, error=type(exc).__name__)
                raise HTTPException(502, detail={"code": "hls_delivery_failed",
                                               "message": "服务端 HLS 清单处理失败"}) from exc
            return Response(
                b"" if request.method == "HEAD" else content,
                media_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store", "Content-Length": str(len(content))},
            )
        if asset.content is not None:
            record("media.manifest", parent_trace=asset.trace, bytes=len(asset.content))
            return Response(
                b"" if request.method == "HEAD" else asset.content,
                media_type=asset.mime, headers={
                    "Cache-Control": "no-store", "Content-Length": str(len(asset.content)),
                },
            )
        start = time.monotonic()
        try:
            upstream = await asyncio.to_thread(
                bridge.open_media, asset, request.method, dict(request.headers),
            )
        except (httpx.HTTPError, PlaybackError) as exc:
            record("media.failed", parent_trace=asset.trace, error=type(exc).__name__)
            raise HTTPException(502, "媒体上游连接失败，请查看链路日志") from exc
        response_headers = {
            name: upstream.headers[name] for name in (
                "content-type", "content-length", "content-range", "accept-ranges",
            ) if name in upstream.headers
        }
        response_headers["Cache-Control"] = "no-store"
        response_headers.setdefault("content-type", asset.mime)
        record("media.upstream", parent_trace=asset.trace, status=upstream.status_code,
               range=bool(request.headers.get("range")),
               duration_ms=round((time.monotonic() - start) * 1000))

        def chunks():
            count = 0
            try:
                if request.method != "HEAD":
                    for chunk in upstream.iter_raw():
                        count += len(chunk)
                        yield chunk
            except httpx.HTTPError as exc:
                record("media.stream_failed", parent_trace=asset.trace, error=type(exc).__name__)
                raise
            finally:
                upstream.close()
                record("media.closed", parent_trace=asset.trace, bytes=count,
                       duration_ms=round((time.monotonic() - start) * 1000))

        return StreamingResponse(
            chunks(), status_code=upstream.status_code, headers=response_headers,
            background=BackgroundTask(upstream.close),
        )

    return router
