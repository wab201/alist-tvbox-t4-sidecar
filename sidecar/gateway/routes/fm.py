from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from sidecar.at.native import NativeAtUnsupportedError
from sidecar.at.subscription import SubscriptionParseError
from sidecar.gateway.fm import translate_fm_request
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.t4.adapter import T4Adapter, T4Request
from sidecar.playback import PlaybackBridge
from sidecar.worker.client import WorkerRemoteError


def build_fm_router(
    source_registry: SourceRegistry,
    adapter: T4Adapter,
    playback: PlaybackBridge | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.api_route(
        "/t4/fm/{source_id}/{plugin_id}/api/{module}",
        methods=["GET", "POST"],
    )
    @router.api_route(
        "/t4/fm/{source_id}/{plugin_id}/",
        methods=["GET", "POST"],
    )
    async def fm_module(
        source_id: str,
        plugin_id: str,
        request: Request,
    ) -> dict[str, Any]:
        if source_registry.get(source_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "source_not_found", "message": "source not found"},
            )

        params = dict(request.query_params)
        if request.method == "POST":
            content_type = request.headers.get("content-type", "").split(";", 1)[0]
            if content_type in {"application/x-www-form-urlencoded", "multipart/form-data"}:
                params.update(dict(await request.form()))
            elif await request.body():
                try:
                    body = await request.json()
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400, detail={"message": "POST body must be JSON or form data"}
                    ) from exc
                if not isinstance(body, dict):
                    raise HTTPException(
                        status_code=400, detail={"message": "POST body must be an object"}
                    )
                params.update(body)
        client_name = request.headers.get("X-CLIENT")
        if client_name and "client" not in params:
            params["client"] = client_name

        try:
            module = request.path_params.get("module")
            if module is None:
                module, params = translate_fm_request(params)
            result = await adapter.dispatch(
                T4Request(
                    module=module,
                    source_id=source_id,
                    plugin_id=plugin_id,
                    params=params,
                    proxy_base=str(request.base_url),
                )
            )
            if module == "play" and playback is not None:
                result = playback.prepare(result, str(request.base_url))
            return result
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
        except WorkerRemoteError as exc:
            raise HTTPException(status_code=502, detail=exc.public_error()) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "t4_backend_failed", "message": type(exc).__name__},
            ) from exc

    return router
