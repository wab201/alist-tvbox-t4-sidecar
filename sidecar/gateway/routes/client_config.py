from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from sidecar.at.subscription import SubscriptionParseError
from sidecar.gateway.fm import (
    build_fm_path,
    build_site_key,
)
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry


def build_client_config_router(
    source_registry: SourceRegistry,
    *,
    subscription_resolver: Any,
    local_snapshot_provider: Any,
) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/client-config")
    async def client_config_link() -> dict[str, str]:
        return {"subscription_path": "/t4/config.json"}

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
                    source.source_id, plugin.plugin_id
                ),
            })
        return sites

    @router.get("/t4/config.json")
    async def client_config(
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        groups = await asyncio.gather(*(
            sites_for(source, str(request.base_url).rstrip("/"))
            for source in source_registry.all()
        ))
        return {"sites": [site for group in groups for site in group]}

    return router
