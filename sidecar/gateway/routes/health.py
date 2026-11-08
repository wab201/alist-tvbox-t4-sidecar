from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sidecar.gateway.config import Settings


class EngineChoice(BaseModel):
    engine: Literal["python", "rust"]


def build_health_router(settings: Settings, engines, traces) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/diagnostics/media-engine")
    async def media_engine() -> dict[str, object]:
        return {**engines.snapshot(), "logging": traces.output_status(), "streams": traces.media_status()}

    @router.put("/t4/api/diagnostics/media-engine")
    async def set_media_engine(body: EngineChoice) -> dict:
        try:
            return await asyncio.to_thread(engines.update, body.engine)
        except RuntimeError as exc:
            raise HTTPException(409, "当前运行方式未加载 Rust 入口，仍使用 Python") from exc
        except OSError as exc:
            traces.emit("runtime.engine_change_failed", error=type(exc).__name__, reason="persist_failed")
            raise HTTPException(503, "引擎设置保存失败，原设置未改变") from exc

    @router.get("/t4/health")
    async def health() -> dict[str, object]:
        return {
            "service": settings.service_name,
            "status": "ok",
            "version": settings.version,
            "worker": {"status": settings.worker_status},
            "media_engine": engines.snapshot()["engine"],
        }

    return router
