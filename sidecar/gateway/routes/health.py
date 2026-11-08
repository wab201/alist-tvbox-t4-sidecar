from __future__ import annotations

from fastapi import APIRouter

from sidecar.gateway.config import Settings


def build_health_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/diagnostics/media-engine")
    async def media_engine() -> dict[str, object]:
        return {"engine": "python", "experimental": False}

    @router.get("/t4/health")
    async def health() -> dict[str, object]:
        return {
            "service": settings.service_name,
            "status": "ok",
            "version": settings.version,
            "worker": {"status": settings.worker_status},
            "media_engine": settings.media_engine,
        }

    return router
