from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from sidecar.at.subscription import SubscriptionParseError
from sidecar.at.native import NativeAtError, NativeAtUnsupportedError
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.gateway.profiles import PluginPolicyDenied, T4ProfileStore
from sidecar.gateway.routes.profiles import require_profile
from sidecar.t4.adapter import T4Adapter, T4Request
from sidecar.playback import PlaybackBridge, PlaybackError
from sidecar.worker.client import WorkerRemoteError, WorkerTimeoutError


def build_t4_router(
    source_registry: SourceRegistry,
    adapter: T4Adapter,
    playback: PlaybackBridge | None = None,
    profiles: T4ProfileStore | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.api_route(
        "/t4/profiles/{profile_id}/api/{module}",
        methods=["GET", "POST"],
    )
    @router.api_route(
        "/t4/api/{module}",
        methods=["GET", "POST"],
    )
    async def t4_module(
        module: str,
        request: Request,
    ) -> dict[str, Any]:
        profile_id = request.path_params.get("profile_id", "default")
        profile = require_profile(profiles, profile_id) if profiles is not None else None
        params = dict(request.query_params)
        if request.method == "POST":
            content_type = request.headers.get("content-type", "").split(";", 1)[0]
            if content_type in {"application/x-www-form-urlencoded", "multipart/form-data"}:
                params.update(dict(await request.form()))
            else:
                try:
                    body = await request.json()
                except ValueError:
                    body = {}
                if isinstance(body, dict):
                    params.update(body)
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
        if profile is not None and plugin_id is not None and not profile.allows(source_id, plugin_id):
            raise HTTPException(403, detail={
                "code": "plugin_disabled", "message": "插件已在此 T4 出口禁用",
            })
        try:
            result = await adapter.dispatch(
                T4Request(
                    module=module,
                    source_id=source_id,
                    plugin_id=plugin_id,
                    params=params,
                    proxy_base=str(request.base_url),
                    profile=profile,
                )
            )
            if module == "play" and playback is not None:
                result = await asyncio.to_thread(playback.prepare, result, str(request.base_url))
            return result
        except PluginPolicyDenied as exc:
            raise HTTPException(403, detail={
                "code": "plugin_disabled", "message": str(exc),
            }) from exc
        except PlaybackError as exc:
            raise HTTPException(status_code=502, detail={
                "code": exc.code, "message": "服务端尚未取得可直接播放的媒体，请查看链路日志",
            }) from exc
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
        except WorkerTimeoutError as exc:
            raise HTTPException(status_code=504, detail={
                "code": "worker_timeout", "message": "服务端插件执行超时，请查看链路日志",
            }) from exc
        except NativeAtError as exc:
            raise HTTPException(status_code=502, detail=exc.public_error()) from exc
        except WorkerRemoteError as exc:
            raise HTTPException(status_code=502, detail=exc.public_error()) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "t4_backend_failed", "message": type(exc).__name__},
            ) from exc

    return router
