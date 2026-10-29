import asyncio
import base64
from contextlib import closing

from fastapi.testclient import TestClient
import httpx
from lxml import etree
import m3u8
import pytest

from sidecar.compatibility.probes import ResourceProbes
from sidecar.playback import PlaybackBridge
from tests.unit.test_hls_delivery import TS, WRAPPED
from tests.unit.test_playback_diagnostics import FakeWorker, MPD, application


class HeaderlessHls(FakeWorker):
    def call(self, runtime_key, plugin, method, params):
        if method == "play":
            return {"url": "https://cdn.example/media.m3u8?sign=private", "parse": 0}
        return super().call(runtime_key, plugin, method, params)


class CountingStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, data, reads, closed):
        self.data, self.reads, self.closed = data, reads, closed

    def __iter__(self):
        for offset in range(0, len(self.data), 4096):
            chunk = self.data[offset:offset + 4096]
            self.reads.append(len(chunk))
            yield chunk

    async def __aiter__(self):
        for chunk in self:
            yield chunk

    def close(self):
        self.closed.append(True)

    async def aclose(self):
        self.close()


def client_app(tmp_path, *, data=TS * 256, headers=None, origin="https://cdn.example", extra=None):
    seen, reads, closed = [], [], []

    class Worker(HeaderlessHls):
        def call(self, runtime_key, plugin, method, params):
            payload = super().call(runtime_key, plugin, method, params)
            if method == "play":
                payload["url"] = origin + "/media.m3u8?sign=private"
                payload["header"] = headers or {}
            return payload

    def respond(request):
        seen.append(request)
        if request.url.path == "/media.m3u8":
            return httpx.Response(200, content=(
                b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\none.ts?sign=private\n#EXT-X-ENDLIST\n"
            ))
        body = b"" if request.method == "HEAD" else data
        return httpx.Response(200, headers={
            "Content-Type": "video/mp2t", "Content-Length": str(len(data)), **(extra or {}),
        }, stream=CountingStream(body, reads, closed))

    app = application(tmp_path, Worker())
    app.state.playback.client.close()
    app.state.playback.client = httpx.Client(transport=httpx.MockTransport(respond))
    app.state.playback.media_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return app, seen, reads, closed


def segment_url(client):
    payload = client.get("/t4/fm/source-a/demo/", params={"play": "raw"}).json()
    listing = client.get(payload["url"])
    return m3u8.loads(listing.text).segments[0].uri, payload


@pytest.mark.parametrize("method,range_value", [
    ("GET", ""), ("GET", "bytes=0-99"), ("GET", "bytes=188-375"),
    ("GET", "bytes=-100"), ("HEAD", ""), ("HEAD", "bytes=0-99"),
])
def test_standard_headerless_segments_redirect_without_relaying_the_body(tmp_path, method, range_value):
    app, seen, reads, closed = client_app(tmp_path)
    with TestClient(app) as client:
        target, payload = segment_url(client)
        headers = {"User-Agent": "real-player", "If-Range": '"example-etag"'}
        if range_value:
            headers["Range"] = range_value
        response = client.request(method, target, headers=headers, follow_redirects=False)
        assert response.status_code == 302 and response.content == b""
        assert response.headers["location"] == "https://cdn.example/one.ts?sign=private"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-length"] == "0"
        assert "content-range" not in response.headers
        assert all(r.headers["user-agent"] == "real-player" for r in seen if r.url.path == "/one.ts")
        assert seen[-1].headers.get("range", "") == range_value
        assert seen[-1].headers["if-range"] == '"example-etag"'
        assert sum(reads) <= 32768
        assert closed
        exported = client.get("/t4/api/diagnostics/export").text
        assert "media.redirect" in exported
        assert all(secret not in exported for secret in ("cdn.example", "private", "real-player"))


@pytest.mark.parametrize("headers", [
    {"Referer": "https://allowed.example"}, {"User-Agent": "signed-agent"},
    {"Cookie": "private-cookie"}, {"Authorization": "private-auth"},
])
def test_header_bound_segments_keep_the_existing_server_path(tmp_path, headers):
    app, _, reads, closed = client_app(tmp_path, data=TS, headers=headers)
    with TestClient(app) as client:
        target, _ = segment_url(client)
        response = client.get(target, follow_redirects=False)
        assert response.status_code == 200 and response.content == TS
        assert "location" not in response.headers
    assert sum(reads) == len(TS) and closed


@pytest.mark.parametrize("origin", ["http://127.0.0.1:5000", "http://192.168.1.2", "http://at:4567"])
def test_internal_resources_are_never_redirected_to_the_client(tmp_path, origin):
    app, _, _, _ = client_app(tmp_path, data=TS, origin=origin)
    transport = app.state.playback.media_client._transport
    app.state.playback._local_media_client = httpx.AsyncClient(transport=transport)
    with TestClient(app) as client:
        target, _ = segment_url(client)
        response = client.get(target, follow_redirects=False)
        assert response.status_code == 200 and response.content == TS


def test_headerless_png_transport_stream_still_gets_unwrapped(tmp_path):
    app, _, _, _ = client_app(tmp_path, data=WRAPPED)
    with TestClient(app) as client:
        target, _ = segment_url(client)
        response = client.get(target, follow_redirects=False)
        assert response.status_code == 200 and response.content == TS
        assert response.headers["content-type"] == "video/mp2t"
        assert "location" not in response.headers


def test_unknown_segment_bytes_are_not_misclassified_as_direct_media(tmp_path):
    data = b"<html>denied</html>" + b"x" * 20000
    app, _, _, _ = client_app(tmp_path, data=data)
    with TestClient(app) as client:
        target, _ = segment_url(client)
        response = client.get(target, follow_redirects=False)
        assert response.status_code == 200 and response.content == data


def test_server_only_set_cookie_keeps_the_existing_media_session(tmp_path):
    app, _, _, _ = client_app(tmp_path, data=TS, extra={"Set-Cookie": "session=server-only"})
    with TestClient(app) as client:
        target, _ = segment_url(client)
        response = client.get(target, follow_redirects=False)
        assert response.status_code == 200 and response.content == TS
        assert "set-cookie" not in response.headers


@pytest.mark.parametrize("async_mode", [False, True])
def test_binary_hls_resources_redirect_before_any_body_read(async_mode):
    reads, closed = [], []
    bridge = PlaybackBridge(transport=httpx.MockTransport(lambda request: httpx.Response(
        206, headers={"Content-Range": "bytes 0-15/16"},
        stream=CountingStream(bytes(16), reads, closed),
    )))
    try:
        root = bridge.prepare({"url": "https://cdn.example/media.m3u8"}, "https://public.example")
        parent = root["url"].rsplit("/", 1)[-1]
        child = bridge._hls_child(parent, "https://cdn.example/key", "binary", "https://public.example")
        asset = bridge.get(child.rsplit("/", 1)[-1])

        async def run():
            response = await bridge.open_media_async(asset, "GET", {"range": "bytes=0-15"}, redirect=True)
            try:
                assert response.status_code == 302
                assert response.headers["location"] == asset.url
            finally:
                await response.aclose()

        if async_mode:
            asyncio.run(run())
        else:
            with closing(bridge.open_media(asset, "GET", {"range": "bytes=0-15"}, redirect=True)) as response:
                assert response.status_code == 302 and response.headers["location"] == asset.url
        assert not reads and closed
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()


def test_http_compatibility_probe_follows_direct_segment_and_checks_bytes(tmp_path):
    app, _, _, _ = client_app(tmp_path, data=TS)
    with TestClient(app) as client:
        _, payload = segment_url(client)
        probes = ResourceProbes(app.state.playback, app.state.playback.client._transport)
        try:
            result = probes.media(payload)
            assert result["status"] == "pass", result
            assert result["real_device_verified"] is False
        finally:
            probes.close()


def test_headerless_inline_dash_only_serves_manifest_and_preserves_cdn_urls():
    bridge = PlaybackBridge()
    try:
        result = bridge.prepare({
            "url": "data:application/dash+xml;base64," + base64.b64encode(MPD).decode(),
            "header": {}, "parse": 0,
        }, "https://public.example")
        manifest = bridge.get(result["url"].rsplit("/", 1)[-1])
        nodes = etree.fromstring(manifest.content).findall(".//{*}BaseURL")
        assert len(nodes) == 2
        assert all(node.text.startswith("https://cdn.example/") for node in nodes)
        assert bridge._size == 1
        bridge.client.close()
        bridge.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            206, content=b"\x00\x00\x00\x20ftyp" + bytes(4088),
        )))
        assert bridge.probe(result)["ok"] is True
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("request_range", [False, True])
def test_rejected_player_agent_keeps_the_working_server_delivery(async_mode, request_range):
    seen = []

    def upstream(request):
        seen.append(request.headers["user-agent"])
        return httpx.Response(
            403 if request.headers["user-agent"] == "unsupported-player" else 200,
            stream=httpx.ByteStream(TS),
        )

    bridge = PlaybackBridge(transport=httpx.MockTransport(upstream))
    try:
        root = bridge.prepare({"url": "https://cdn.example/media.m3u8"}, "https://public.example")
        parent = root["url"].rsplit("/", 1)[-1]
        child = bridge._hls_child(parent, "https://cdn.example/one.ts", "segment", "https://public.example")
        asset = bridge.get(child.rsplit("/", 1)[-1])
        headers = {"user-agent": "unsupported-player"}
        if request_range:
            headers["range"] = "bytes=0-99"

        async def run():
            response = await bridge.open_media_async(
                asset, "GET", headers, redirect=True,
            )
            try:
                assert response.status_code == 200 and await response.aread() == TS
            finally:
                await response.aclose()

        if async_mode:
            asyncio.run(run())
        else:
            with closing(bridge.open_media(
                asset, "GET", headers, redirect=True,
            )) as response:
                assert response.status_code == 200 and response.read() == TS
        assert len(seen) == (3 if request_range else 2) and seen[0] == "unsupported-player"
        assert all(agent.startswith("python-httpx/") for agent in seen[1:])
        assert asset.redirectable is False
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()
