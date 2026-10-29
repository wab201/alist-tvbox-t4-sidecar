from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
import httpx

from sidecar.diagnostics import record
from sidecar.gateway.streaming import ManagedStreamingResponse
from sidecar.plugin_proxy import PluginProxyRegistry, PluginProxyTarget, is_proxy_text, rewrite_chunks
from sidecar.worker.host import HOP_HEADERS


class PluginProxyResponse(ManagedStreamingResponse):
    def __init__(self, target: PluginProxyTarget, key: str, request: Request, client: httpx.AsyncClient):
        super().__init__(parent_trace=target.trace, stage="proxy")
        self.target, self.key, self.request, self.client = target, key, request, client

    async def stream_response(self, send):
        target, request, client = self.target, self.request, self.client
        headers = {name: value for name, value in request.headers.items()
                   if name.lower() not in HOP_HEADERS | {"host", "accept-encoding"}}
        headers["Accept-Encoding"] = "identity"
        try:
            self.upstream = await client.send(
                client.build_request(request.method, target.url, params=request.query_params, headers=headers),
                stream=True, follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            self.reason = "upstream_error"
            record("proxy.failed", parent_trace=target.trace, error=type(exc).__name__)
            raise HTTPException(502, "插件代理连接失败，请重新打开详情或查看日志") from exc
        upstream = self.upstream
        response_headers = {
            name: value for name, value in upstream.headers.items()
            if name.lower() not in HOP_HEADERS | {"server", "date"}
        }
        response_headers["cache-control"] = "no-store"
        public = str(request.base_url).rstrip("/") + "/t4/proxy/" + self.key
        if "location" in response_headers:
            response_headers["location"] = response_headers["location"].replace(target.url, public)
        mime = response_headers.get("content-type", "").split(";")[0].strip().lower()
        rewrite = is_proxy_text(mime) and not response_headers.get("content-encoding")
        if rewrite:
            for name in ("content-length", "content-range", "accept-ranges", "etag", "last-modified"):
                response_headers.pop(name, None)
        record("proxy.upstream", parent_trace=target.trace, status=upstream.status_code,
               range=bool(request.headers.get("range")))

        async def chunks():
            if request.method == "HEAD":
                return
            stream = upstream.aiter_raw()
            if rewrite:
                stream = rewrite_chunks(stream, target.url.encode(), public.encode())
            async for chunk in stream:
                self.count += len(chunk)
                yield chunk

        self.status_code = upstream.status_code
        self.init_headers(response_headers)
        self.body_iterator = chunks()
        try:
            await super().stream_response(send)
        except httpx.HTTPError as exc:
            self.reason = "upstream_error"
            record("proxy.stream_failed", parent_trace=target.trace, error=type(exc).__name__)
            raise
        self.reason = "complete"


def build_plugin_proxy_router(proxies: PluginProxyRegistry, client: httpx.AsyncClient) -> APIRouter:
    router = APIRouter()

    @router.api_route("/t4/proxy/{key}", methods=["GET", "HEAD"])
    async def proxy(key: str, request: Request):
        target = proxies.get(key)
        if target is None:
            raise HTTPException(410, "插件代理会话已过期，请刷新页面后重试")
        return PluginProxyResponse(target, key, request, client)

    return router
