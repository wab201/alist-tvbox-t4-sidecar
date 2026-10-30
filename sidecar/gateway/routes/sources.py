from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from sidecar.gateway.playback_settings import PlaybackSettingsStore
from sidecar.gateway.navigation_settings import NavigationSettingsStore, can_search
from sidecar.at.subscription import SubscriptionParseError

from sidecar.gateway.source_registry import SourceRegistry


class PlaybackSettingsInput(BaseModel):
    mode: str
    at_internal_url: str = ""

class NavigationSettingsInput(BaseModel):
    plugin_ids: list[str] | None = None


def build_sources_router(
    source_registry: SourceRegistry,
    playback_settings: PlaybackSettingsStore | None = None,
    navigation_settings: NavigationSettingsStore | None = None,
    snapshot_resolver=None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/sources")
    async def list_sources() -> dict[str, object]:
        return {"sources": source_registry.public_metadata()}

    if navigation_settings is not None and snapshot_resolver is not None:
        async def navigation(source_id, body=None):
            source = source_registry.get(source_id)
            if source is None:
                raise HTTPException(404, "订阅不存在")
            try:
                snapshot = await snapshot_resolver(source)
            except SubscriptionParseError as exc:
                raise HTTPException(502, "订阅暂不可用，请刷新后重试") from exc
            plugins = [p for p in snapshot.plugins if can_search(p)]
            ids = {p.plugin_id for p in plugins}
            if body is not None:
                if body.plugin_ids is not None and not set(body.plugin_ids) <= ids:
                    raise HTTPException(400, "包含已移除或不可搜索的插件，请刷新后重选")
                try:
                    navigation_settings.update(source_id, body.plugin_ids)
                except OSError as exc:
                    raise HTTPException(500, "搜源配置保存失败，请检查数据目录权限") from exc
            selected = navigation_settings.selection(source_id)
            return {
                "plugin_ids": selected, "concurrency": 3, "budget_seconds": 25,
                "plugins": [{"id": p.plugin_id, "name": p.name,
                             "selected": selected is None or p.plugin_id in selected}
                            for p in plugins],
            }

        @router.get("/t4/api/sources/{source_id}/navigation")
        async def get_navigation(source_id: str, response: Response) -> dict:
            response.headers["Cache-Control"] = "no-store"
            return await navigation(source_id)

        @router.put("/t4/api/sources/{source_id}/navigation")
        async def update_navigation(
            source_id: str, body: NavigationSettingsInput, response: Response,
        ) -> dict:
            response.headers["Cache-Control"] = "no-store"
            return await navigation(source_id, body)

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
