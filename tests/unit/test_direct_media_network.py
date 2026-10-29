import re
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import Response
import httpx
import pytest

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry
from tests.unit.test_hls_delivery import TS
from tests.unit.test_media_disconnect import serving


@pytest.mark.parametrize("method,range_value", [
    ("GET", ""), ("GET", "bytes=188-375"), ("GET", "bytes=-188"), ("HEAD", ""),
])
def test_real_http_redirect_preserves_player_range_bytes_and_avoids_gateway_body(
    tmp_path, monkeypatch, method, range_value,
):
    data = TS * 64
    seen = []
    upstream = FastAPI()

    @upstream.api_route("/segment.ts", methods=["GET", "HEAD"])
    async def segment(request: Request):
        assert request.headers["user-agent"] == "socket-player"
        seen.append((request.method, request.headers.get("range", "")))
        start, end, status = 0, len(data) - 1, 200
        headers = {"Accept-Ranges": "bytes", "ETag": '"same-object"'}
        if request.headers.get("range"):
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers["range"])
            if match[1]:
                start, end = int(match[1]), int(match[2]) if match[2] else end
            else:
                start = len(data) - int(match[2])
            end = min(end, len(data) - 1)
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        headers["Content-Length"] = str(end - start + 1)
        return Response(b"" if request.method == "HEAD" else data[start:end + 1],
                        status_code=status, headers=headers, media_type="video/mp2t")

    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with serving(upstream) as cdn, serving(app) as gateway:
        bridge = app.state.playback
        # Only the socket fixture uses a loopback CDN; production rejects local redirects.
        monkeypatch.setattr(bridge, "_direct_target", lambda url: urlsplit(url).netloc == urlsplit(cdn).netloc)
        root = bridge.prepare({"url": cdn + "/media.m3u8"}, gateway)
        parent = root["url"].rsplit("/", 1)[-1]
        target = bridge._hls_child(parent, cdn + "/segment.ts", "segment", gateway)
        headers = {"User-Agent": "socket-player", "If-Range": '"same-object"'}
        if range_value:
            headers["Range"] = range_value
        with httpx.Client(trust_env=False, timeout=10, follow_redirects=True) as client:
            response = client.request(method, target, headers=headers)
        assert len(response.history) == 1 and response.history[0].status_code == 302
        assert str(response.url) == cdn + "/segment.ts"
        assert response.status_code == (206 if range_value else 200)
        expected = data[188:376] if range_value == "bytes=188-375" else (
            data[-188:] if range_value else data
        )
        assert response.content == (b"" if method == "HEAD" else expected)
        assert response.headers["Content-Length"] == str(len(expected))
        assert seen[-1] == (method, range_value)
        events = app.state.traces.events()
        closed = [event for event in events if event["stage"] == "media.closed"]
        assert closed and closed[-1]["bytes"] == 0
        assert any(event["stage"] == "media.redirect" for event in events)
