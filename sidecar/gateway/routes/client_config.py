from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, Response

from sidecar.at.subscription import SubscriptionParseError
from sidecar.gateway.auth import gateway_token_matches
from sidecar.gateway.config import Settings
from sidecar.gateway.fm import (
    build_config_path,
    build_fm_path,
    build_site_key,
    config_access_key_matches,
)
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry


def build_client_config_router(
    settings: Settings,
    source_registry: SourceRegistry,
    *,
    subscription_resolver: Any,
    local_snapshot_provider: Any,
) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/client-config")
    async def client_config_link(
        gateway_token: Annotated[str | None, Header(alias="X-T4-Token")] = None,
    ) -> dict[str, str]:
        if not gateway_token_matches(settings.gateway_token, gateway_token):
            raise HTTPException(status_code=401, detail={"message": "gateway token required"})
        return {"subscription_path": build_config_path(settings.gateway_token)}

    async def sites_for(source: SourceConfig, base_url: str) -> list[dict[str, Any]]:
        snapshot = local_snapshot_provider(source.source_id)
        if snapshot is None:
            try:
                snapshot = await subscription_resolver(source)
            except SubscriptionParseError as exc:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "subscription_unavailable",
                        "message": f"source {source.source_id} is unavailable",
                    },
                ) from exc
        sites = []
        seen = set()
        for plugin in snapshot.plugins:
            if plugin.public_metadata().get("t4_supported", True) is False:
                continue
            key = build_site_key(source.source_id, plugin.plugin_id)
            if key in seen:
                continue
            seen.add(key)
            sites.append({
                "searchable": 1,
                "quickSearch": 1,
                "filterable": 1,
                **plugin.site_options,
                "key": key,
                "name": plugin.name,
                "type": 4,
                "api": base_url + build_fm_path(
                    settings.gateway_token, source.source_id, plugin.plugin_id
                ),
            })
        return sites

    @router.get("/t4/config/{access_key}.json")
    @router.get("/t4/config.json")
    async def client_config(
        request: Request,
        response: Response,
        access_key: str | None = None,
        gateway_token: Annotated[str | None, Header(alias="X-T4-Token")] = None,
    ) -> dict[str, Any]:
        if settings.gateway_token and not (
            config_access_key_matches(settings.gateway_token, access_key)
            or gateway_token_matches(settings.gateway_token, gateway_token)
        ):
            raise HTTPException(status_code=401, detail={"message": "gateway token required"})
        response.headers["Cache-Control"] = "no-store"
        groups = await asyncio.gather(*(
            sites_for(source, str(request.base_url).rstrip("/"))
            for source in source_registry.all()
        ))
        return {"sites": [site for group in groups for site in group]}

    return router
