from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from sidecar.at.subscription import SubscriptionParseError
from sidecar.gateway.fm import build_site_key
from sidecar.gateway.profiles import T4ProfileStore
from sidecar.gateway.source_registry import SourceRegistry


class ProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    mode: Literal["all", "whitelist", "blacklist"] = "all"
    whitelist: list[str] = Field(default_factory=list, max_length=20000)
    blacklist: list[str] = Field(default_factory=list, max_length=20000)


def require_profile(store: T4ProfileStore, identifier: str):
    profile = store.get(identifier)
    if profile is None or not profile.enabled:
        raise HTTPException(404, detail={
            "code": "profile_unavailable", "message": "T4 出口不存在或已停用",
        })
    return profile


def build_profiles_router(
    store: T4ProfileStore, sources: SourceRegistry, snapshot_resolver,
) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/profiles")
    def list_profiles(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return {"profiles": [profile.public_metadata() for profile in store.all()]}

    def save(identifier, body):
        try:
            return {"profile": store.save(identifier, body.model_dump()).public_metadata()}
        except KeyError as exc:
            raise HTTPException(404, "T4 出口不存在") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, "出口配置保存失败，请检查数据目录权限") from exc

    @router.post("/t4/api/profiles", status_code=201)
    def create_profile(body: ProfileInput) -> dict:
        return save(None, body)

    @router.put("/t4/api/profiles/{profile_id}")
    def update_profile(profile_id: str, body: ProfileInput) -> dict:
        return save(profile_id, body)

    @router.delete("/t4/api/profiles/{profile_id}")
    def delete_profile(profile_id: str) -> dict:
        try:
            store.delete(profile_id)
        except KeyError as exc:
            raise HTTPException(404, "T4 出口不存在") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, "出口配置保存失败，请检查数据目录权限") from exc
        return {"deleted": True}

    @router.get("/t4/api/profiles/{profile_id}/plugins")
    async def profile_plugins(profile_id: str, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        profile = store.get(profile_id)
        if profile is None:
            raise HTTPException(404, "T4 出口不存在")
        plugins, errors = [], []
        for source in sources.all():
            try:
                snapshot = await snapshot_resolver(source)
            except SubscriptionParseError:
                errors.append({"source_id": source.source_id, "message": "订阅暂不可用"})
                continue
            for plugin in snapshot.plugins:
                metadata = plugin.public_metadata()
                supported = bool(plugin.native_client_site) or metadata.get("t4_supported", True)
                plugins.append({
                    "key": build_site_key(source.source_id, plugin.plugin_id),
                    "source_id": source.source_id, "source_name": source.display_name,
                    "plugin_id": plugin.plugin_id, "name": plugin.name,
                    "supported": supported,
                    "enabled": bool(supported and profile.allows(source.source_id, plugin.plugin_id)),
                    "native_client": bool(plugin.native_client_site),
                })
        return {"plugins": plugins, "errors": errors}

    return router
