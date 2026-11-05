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
from sidecar.gateway.profiles import T4ProfileStore
from sidecar.gateway.routes.profiles import require_profile
from sidecar.client_acceleration import ClientAcceleration


def build_client_config_router(
    source_registry: SourceRegistry,
    *,
    subscription_resolver: Any,
    local_snapshot_provider: Any,
    profiles: T4ProfileStore,
    acceleration: ClientAcceleration | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/client-config")
    async def client_config_link() -> dict[str, str]:
        return {"subscription_path": "/t4/config.json"}

    async def sites_for(source: SourceConfig, base_url: str, profile) -> tuple[
        list[dict[str, Any]], list[tuple[str, str, dict[str, Any]]],
    ]:
        if profile.mode == "whitelist" and not profile.whitelist:
            return [], []
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
        sites, native_sites = [], []
        seen = set()
        for plugin in snapshot.plugins:
            if not profile.allows(source.source_id, plugin.plugin_id):
                continue
            if plugin.native_client_site:
                native_sites.append((
                    source.source_id, plugin.plugin_id, dict(plugin.native_client_site),
                ))
                continue
            if plugin.public_metadata().get("t4_supported", True) is False:
                continue
            key = build_site_key(source.source_id, plugin.plugin_id)
            if key in seen:
                continue
            seen.add(key)
            options = dict(plugin.site_options)
            if plugin.api == "csp_PianDan":
                options.update(
                    indexs=1, searchable=0, quickSearch=0, filterable=1
                )
            sites.append({
                "searchable": 1,
                "quickSearch": 1,
                "filterable": 1,
                **options,
                "key": key,
                "name": plugin.name,
                "type": 4,
                "api": base_url + build_fm_path(
                    source.source_id, plugin.plugin_id, profile.id
                ),
            })
        return sites, native_sites

    @router.get("/t4/config.json")
    @router.get("/t4/profiles/{profile_id}/config.json")
    async def client_config(
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        profile_id = request.path_params.get("profile_id", "default")
        profile = require_profile(profiles, profile_id)
        response.headers["Cache-Control"] = "no-store"
        groups = await asyncio.gather(*(
            sites_for(source, str(request.base_url).rstrip("/"), profile)
            for source in source_registry.all()
        ))
        sites, native_sites = [], []
        seen = set()
        for group, native_group in groups:
            for site in group:
                if site["key"] not in seen:
                    sites.append(site)
                    seen.add(site["key"])
            for source_id, plugin_id, site in native_group:
                if site["key"] not in seen:
                    native_sites.append((source_id, plugin_id, site))
                    seen.add(site["key"])
        output = {"sites": sites}
        if (
            acceleration is not None
            and (sites or native_sites)
        ):
            session = acceleration.offer(
                str(request.base_url), profile_id, native_sites=tuple(native_sites),
            )
            if session is not None:
                # Keep optional loaders out of the import-critical JSON contract.
                response.headers["Link"] = (
                    f'<{session.base}/t4/acceleration/{session.id}/component.json>; '
                    'rel="urn:t4-sidecar:client-component"; type="application/json"'
                )
                for site in sites:
                    site["api"] += "?t4_client=" + session.id
        return output

    return router
