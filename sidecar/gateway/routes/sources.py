from __future__ import annotations

from fastapi import APIRouter

from sidecar.gateway.source_registry import SourceRegistry


def build_sources_router(
    source_registry: SourceRegistry,
) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/sources")
    async def list_sources() -> dict[str, object]:
        return {"sources": source_registry.public_metadata()}

    return router
