from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Header, HTTPException, UploadFile

from sidecar.gateway.auth import gateway_token_matches
from sidecar.gateway.config import Settings
from sidecar.gateway.fm import build_fm_path
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.gateway.uploads import PluginUploadError, UploadedPluginStore


def build_uploads_router(
    settings: Settings,
    source_registry: SourceRegistry,
    plugin_store: UploadedPluginStore,
) -> APIRouter:
    router = APIRouter()

    @router.post("/t4/api/plugins/upload")
    async def upload_plugin(
        file: UploadFile = File(...),
        gateway_token: Annotated[str | None, Header(alias="X-T4-Token")] = None,
    ) -> dict[str, object]:
        if not gateway_token_matches(settings.gateway_token, gateway_token):
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "gateway token required"},
            )
        try:
            content = await file.read(settings.upload_max_bytes + 1)
            plugin = plugin_store.register(file.filename, content)
        except PluginUploadError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "plugin_upload_invalid", "message": str(exc)},
            ) from exc
        source_registry.upsert(plugin_store.source_config)
        metadata = plugin.public_metadata()
        metadata["fm_path"] = build_fm_path(
            settings.gateway_token,
            plugin_store.source_config.source_id,
            plugin.plugin_id,
        )
        return {
            "source_id": plugin_store.source_config.source_id,
            "plugin": metadata,
        }

    @router.delete("/t4/api/plugins/{plugin_id}")
    async def delete_plugin(
        plugin_id: str,
        gateway_token: Annotated[str | None, Header(alias="X-T4-Token")] = None,
    ) -> dict[str, object]:
        if not gateway_token_matches(settings.gateway_token, gateway_token):
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "gateway token required"},
            )
        if not plugin_store.remove(plugin_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "plugin_not_found", "message": "plugin not found"},
            )
        if not plugin_store.has_plugins():
            source_registry.remove(plugin_store.source_config.source_id)
        return {"deleted": True, "plugin_id": plugin_id}

    return router
