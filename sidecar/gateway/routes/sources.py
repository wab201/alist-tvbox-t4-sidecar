from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException

from sidecar.gateway.auth import gateway_token_matches
from sidecar.gateway.config import Settings
from sidecar.gateway.source_registry import SourceRegistry


def build_sources_router(
    settings: Settings,
    source_registry: SourceRegistry,
) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/sources")
    async def list_sources(
        gateway_token: Annotated[str | None, Header(alias="X-T4-Token")] = None,
    ) -> dict[str, object]:
        if not gateway_token_matches(settings.gateway_token, gateway_token):
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "unauthorized",
                    "message": "gateway token required",
                },
            )
        return {"sources": source_registry.public_metadata()}

    return router
