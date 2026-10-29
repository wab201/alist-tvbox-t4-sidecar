from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from sidecar.gateway.playback_settings import PlaybackSettingsStore

from sidecar.gateway.source_registry import SourceRegistry


class PlaybackSettingsInput(BaseModel):
    mode: str
    at_internal_url: str = ""


def build_sources_router(
    source_registry: SourceRegistry,
    playback_settings: PlaybackSettingsStore | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/sources")
    async def list_sources() -> dict[str, object]:
        return {"sources": source_registry.public_metadata()}

    if playback_settings is not None:
        @router.get("/t4/api/sources/{source_id}/playback")
        def get_playback(source_id: str, response: Response) -> dict:
            response.headers["Cache-Control"] = "no-store"
            try:
                return playback_settings.get(source_id)
            except KeyError as exc:
                raise HTTPException(404, "订阅不存在") from exc

        @router.put("/t4/api/sources/{source_id}/playback")
        def update_playback(source_id: str, body: PlaybackSettingsInput, response: Response) -> dict:
            response.headers["Cache-Control"] = "no-store"
            try:
                return playback_settings.update(source_id, body.mode, body.at_internal_url)
            except KeyError as exc:
                raise HTTPException(404, "订阅不存在") from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except OSError as exc:
                raise HTTPException(500, "播放配置保存失败，请检查数据目录权限") from exc

    return router
