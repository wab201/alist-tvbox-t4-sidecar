from __future__ import annotations

import asyncio
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict

from sidecar.at.subscription import PluginDescriptor
from sidecar.gateway.config import Settings
from sidecar.gateway.fm import build_fm_path
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.gateway.uploads import PluginUploadError, UploadedPluginStore
from sidecar.t4.adapter import T4Adapter


class RenamePlugin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class ReorderPlugins(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_ids: list[str]


def build_uploads_router(
    settings: Settings,
    source_registry: SourceRegistry,
    plugin_store: UploadedPluginStore,
    adapter: T4Adapter,
) -> APIRouter:
    router = APIRouter()

    def metadata(plugin: PluginDescriptor) -> dict[str, object]:
        result = plugin.public_metadata()
        result["fm_path"] = build_fm_path(
            plugin_store.source_config.source_id,
            plugin.plugin_id,
        )
        return result

    def invalid(exc: PluginUploadError) -> HTTPException:
        return HTTPException(
            status_code=400,
            detail={"code": "plugin_upload_invalid", "message": str(exc)},
        )

    def not_found() -> HTTPException:
        return HTTPException(
            status_code=404,
            detail={"code": "plugin_not_found", "message": "插件不存在或已删除"},
        )

    @router.get("/t4/api/plugins")
    async def list_plugins() -> dict[str, object]:
        snapshot = plugin_store.snapshot_for(plugin_store.source_config.source_id)
        return {"plugins": [metadata(plugin) for plugin in snapshot.plugins]}

    @router.post("/t4/api/plugins/reorder")
    async def reorder_plugins(
        body: ReorderPlugins,
    ) -> dict[str, object]:
        try:
            await asyncio.to_thread(plugin_store.reorder, body.plugin_ids)
        except PluginUploadError as exc:
            raise invalid(exc) from exc
        return {"reordered": True, "plugin_ids": body.plugin_ids}

    @router.post("/t4/api/plugins/upload")
    async def upload_plugin(
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        try:
            content = await file.read(settings.upload_max_bytes + 1)
            plugin = await asyncio.to_thread(plugin_store.register, file.filename, content)
        except PluginUploadError as exc:
            raise invalid(exc) from exc
        source_registry.upsert(plugin_store.source_config)
        return {
            "source_id": plugin_store.source_config.source_id,
            "plugin": metadata(plugin),
        }

    @router.patch("/t4/api/plugins/{plugin_id}")
    async def rename_plugin(
        plugin_id: str,
        body: RenamePlugin,
    ) -> dict[str, object]:
        try:
            plugin = await asyncio.to_thread(plugin_store.rename, plugin_id, body.name)
        except KeyError as exc:
            raise not_found() from exc
        except PluginUploadError as exc:
            raise invalid(exc) from exc
        return {"plugin": metadata(plugin)}

    @router.post("/t4/api/plugins/{plugin_id}/refresh")
    async def refresh_plugin(
        plugin_id: str,
    ) -> dict[str, object]:
        source_id = plugin_store.source_config.source_id
        async with adapter.plugin_lock(source_id, plugin_id):
            try:
                plugin = await asyncio.to_thread(plugin_store.validate, plugin_id)
            except KeyError as exc:
                raise not_found() from exc
            except PluginUploadError as exc:
                raise invalid(exc) from exc
            await adapter.reset_plugin(source_id, plugin_id)
        return {"refreshed": True, "plugin": metadata(plugin)}

    @router.delete("/t4/api/plugins/{plugin_id}")
    async def delete_plugin(
        plugin_id: str,
    ) -> dict[str, object]:
        source_id = plugin_store.source_config.source_id
        async with adapter.plugin_lock(source_id, plugin_id):
            if not await asyncio.to_thread(plugin_store.remove, plugin_id):
                raise not_found()
            await adapter.reset_plugin(source_id, plugin_id)
            if not plugin_store.has_plugins():
                source_registry.remove(source_id)
        return {"deleted": True, "plugin_id": plugin_id}

    return router
