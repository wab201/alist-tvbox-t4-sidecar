from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request

from sidecar.at.native import NativeAtUnsupportedError
from sidecar.at.subscription import SubscriptionParseError
from sidecar.gateway.auth import gateway_token_matches
from sidecar.gateway.config import Settings
from sidecar.gateway.fm import fm_access_key_matches
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.t4.adapter import T4Adapter, T4Request


def build_fm_router(
    settings: Settings,
    source_registry: SourceRegistry,
    adapter: T4Adapter,
) -> APIRouter:
    router = APIRouter()

    @router.api_route(
        "/t4/fm/{source_id}/{plugin_id}/{access_key}/api/{module}",
        methods=["GET", "POST"],
    )
    async def fm_module(
        source_id: str,
        plugin_id: str,
        access_key: str,
        module: str,
        request: Request,
        gateway_token: Annotated[str | None, Header(alias="X-T4-Token")] = None,
    ) -> dict[str, Any]:
        if not fm_access_key_matches(
            settings.gateway_token,
            source_id,
            plugin_id,
            access_key,
        ):
            if not gateway_token_matches(settings.gateway_token, gateway_token):
                raise HTTPException(
                    status_code=401,
                    detail={
                        "code": "unauthorized",
                        "message": "FM compatibility address is invalid",
                    },
                )

        if source_registry.get(source_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "source_not_found", "message": "source not found"},
            )

        params = dict(request.query_params)
        if request.method == "POST":
            try:
                body = await request.json()
            except ValueError:
                body = {}
            if isinstance(body, dict):
                params.update(body)
        params.pop("t4_token", None)
        client_name = request.headers.get("X-CLIENT")
        if client_name and "client" not in params:
            params["client"] = client_name

        try:
            return await adapter.dispatch(
                T4Request(
                    module=module,
                    source_id=source_id,
                    plugin_id=plugin_id,
                    params=params,
                )
            )
        except SubscriptionParseError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "t4_request_invalid", "message": str(exc)},
            ) from exc
        except NativeAtUnsupportedError as exc:
            raise HTTPException(
                status_code=501,
                detail={
                    "code": "t4_builtin_unsupported",
                    "message": str(exc),
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "t4_backend_failed", "message": type(exc).__name__},
            ) from exc

    return router
