from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from sidecar.diagnostics import record, trace_id
from sidecar.playback import PlaybackBridge


def raw_asset(bridge: PlaybackBridge, key: str) -> tuple[dict | None, str]:
    asset = bridge.get(key)
    if asset is None:
        raise HTTPException(410, "Media expired")
    if asset.content is not None or asset.kind not in {"binary", "at_proxy"}:
        return None, "python_transform"
    if asset.redirectable:
        return None, "direct_policy"
    url = asset.url
    resolved = bridge._proxies.resolve(url) if bridge._proxies is not None else None
    local = bool(resolved or asset.local_transport or urlsplit(url).hostname in {
        "127.0.0.1", "::1", "localhost",
    })
    if resolved:
        url = resolved
    if urlsplit(url).scheme not in {"http", "https"}:
        return None, "python_transport"
    # Preserve HTTPX proxy/session semantics instead of silently changing routes.
    if not local and any(os.getenv(name) for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    )):
        return None, "python_proxy"
    client = bridge._local_media_client if local else bridge.media_client
    headers = dict(client.build_request("GET", url, headers=bridge._media_headers(asset, {})).headers)
    if "cookie" in headers and not any(k.lower() == "cookie" for k in asset.headers):
        return None, "python_session"
    return {
        "url": url, "headers": headers, "mime": asset.mime,
        "trace_id": trace_id(), "parent_trace": asset.trace,
    }, "raw_bytes"


def build_media_engine_router(bridge: PlaybackBridge) -> APIRouter:
    router = APIRouter()

    @router.get("/__t4_relay/asset/{key}", include_in_schema=False)
    async def resolve(key: str, request: Request):
        if request.client is None or request.client.host != "127.0.0.1":
            raise HTTPException(404)
        result, reason = raw_asset(bridge, key)
        record("media.engine", engine="rust" if result else "python", reason=reason,
               parent_trace=result.get("parent_trace") if result else None)
        if result is None:
            return Response(status_code=204)
        return result

    return router
