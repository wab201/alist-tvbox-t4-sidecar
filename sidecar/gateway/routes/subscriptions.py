from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException

from sidecar.at.subscription import SubscriptionParseError, SubscriptionFetcher, resolve_subscription
from sidecar.gateway.auth import gateway_token_matches
from sidecar.gateway.config import Settings
from sidecar.gateway.fm import build_fm_path
from sidecar.gateway.source_registry import SourceRegistry


def build_subscriptions_router(
    settings: Settings,
    source_registry: SourceRegistry,
    fetcher: SubscriptionFetcher | None = None,
    *,
    subscription_resolver: Any | None = None,
    subscription_refresher: Any | None = None,
    local_snapshot_provider: Any | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/sources/{source_id}/subscription")
    async def get_subscription_snapshot(
        source_id: str,
        refresh: bool = False,
        gateway_token: Annotated[str | None, Header(alias="X-T4-Token")] = None,
    ) -> dict[str, object]:
        if not gateway_token_matches(settings.gateway_token, gateway_token):
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "gateway token required"},
            )

        source = source_registry.get(source_id)
        if source is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "source_not_found", "message": "source not found"},
            )
        try:
            snapshot = None
            if local_snapshot_provider is not None:
                snapshot = local_snapshot_provider(source_id)
            if snapshot is None:
                if refresh and subscription_refresher is not None:
                    snapshot = await subscription_refresher(source)
                elif subscription_resolver is not None:
                    snapshot = await subscription_resolver(source)
                else:
                    snapshot = await resolve_subscription(source, fetcher)
        except SubscriptionParseError as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "subscription_unavailable", "message": str(exc)},
            ) from exc
        metadata = snapshot.public_metadata()
        for plugin in metadata["plugins"]:
            if plugin.get("t4_supported", True) is not False:
                plugin["fm_path"] = build_fm_path(
                    settings.gateway_token,
                    source_id,
                    str(plugin["id"]),
                )
        return metadata

    return router
