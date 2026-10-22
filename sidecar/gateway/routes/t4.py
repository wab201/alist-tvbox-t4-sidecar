from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request

from sidecar.at.subscription import SubscriptionParseError
from sidecar.at.native import NativeAtUnsupportedError
from sidecar.gateway.auth import gateway_token_matches
from sidecar.gateway.config import Settings
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.t4.adapter import T4Adapter, T4Request


def build_t4_router(
    settings: Settings,
    source_registry: SourceRegistry,
    adapter: T4Adapter,
) -> APIRouter:
    router = APIRouter()

    @router.api_route(
        "/t4/api/{module}",
        methods=["GET", "POST"],
    )
    async def t4_module(
        module: str,
        request: Request,
        gateway_token: Annotated[str | None, Header(alias="X-T4-Token")] = None,
    ) -> dict[str, Any]:
        if not gateway_token_matches(settings.gateway_token, gateway_token):
            gateway_token = request.query_params.get("t4_token")
        if not gateway_token_matches(settings.gateway_token, gateway_token):
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "gateway token required"},
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

        source_id = str(params.pop("source", "") or "")
        if not source_id:
            raise HTTPException(
                status_code=400,
                detail={"code": "source_required", "message": "source is required"},
            )
        plugin_id = str(params.pop("site", "") or params.pop("plugin", "") or "") or None
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
