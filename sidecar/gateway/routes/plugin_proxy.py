from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
import httpx
from starlette.background import BackgroundTask

from sidecar.diagnostics import record
from sidecar.plugin_proxy import PluginProxyRegistry, is_proxy_text, rewrite_chunks
from sidecar.worker.host import HOP_HEADERS


def build_plugin_proxy_router(proxies: PluginProxyRegistry, client: httpx.Client) -> APIRouter:
    router = APIRouter()

    @router.api_route("/t4/proxy/{key}", methods=["GET", "HEAD"])
    async def proxy(key: str, request: Request):
        target = proxies.get(key)
        if target is None:
            raise HTTPException(410, "插件代理会话已过期，请刷新页面后重试")
        headers = {name: value for name, value in request.headers.items()
                   if name.lower() not in HOP_HEADERS | {"host", "accept-encoding"}}
        headers["Accept-Encoding"] = "identity"
        try:
            upstream = await asyncio.to_thread(
                client.send, client.build_request(
                    request.method, target.url, params=request.query_params, headers=headers,
                ), stream=True, follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            record("proxy.failed", parent_trace=target.trace, error=type(exc).__name__)
            raise HTTPException(502, "插件代理连接失败，请重新打开详情或查看日志") from exc
        response_headers = {
            name: value for name, value in upstream.headers.items()
            if name.lower() not in HOP_HEADERS | {"server", "date"}
        }
        response_headers["cache-control"] = "no-store"
        public = str(request.base_url).rstrip("/") + "/t4/proxy/" + key
        if "location" in response_headers:
            response_headers["location"] = response_headers["location"].replace(target.url, public)
        mime = response_headers.get("content-type", "").split(";")[0].strip().lower()
        rewrite = is_proxy_text(mime) and not response_headers.get("content-encoding")
        if rewrite:
            for name in ("content-length", "content-range", "accept-ranges", "etag"):
                response_headers.pop(name, None)
        record("proxy.upstream", parent_trace=target.trace, status=upstream.status_code,
               range=bool(request.headers.get("range")))

        def chunks():
            count = 0
            try:
                if request.method == "HEAD":
                    return
                stream = upstream.iter_raw(65536)
                if rewrite:
                    stream = rewrite_chunks(stream, target.url.encode(), public.encode())
                for chunk in stream:
                    count += len(chunk)
                    yield chunk
            except httpx.HTTPError as exc:
                record("proxy.stream_failed", parent_trace=target.trace, error=type(exc).__name__)
                raise
            finally:
                upstream.close()
                record("proxy.closed", parent_trace=target.trace, bytes=count)

        return StreamingResponse(
            chunks(), status_code=upstream.status_code, headers=response_headers,
            background=BackgroundTask(upstream.close),
        )

    return router
