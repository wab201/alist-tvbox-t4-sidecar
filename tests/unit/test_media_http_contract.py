import time

import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.playback import MediaAsset


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("status", [200, 206, 416])
def test_untouched_media_method_range_validators_and_large_lengths(tmp_path, method, status):
    size = 5 * 1024 ** 3
    seen = []
    closed = []
    requested_range = "bytes=100-199"
    content = b"x" * 100
    length = size if method == "HEAD" and status == 200 else len(content) if status != 416 else 0

    class Stream(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            if method == "HEAD":
                raise AssertionError("a HEAD response must not consume the upstream body")
            yield content if status != 416 else b""

        def close(self):
            closed.append(True)

        async def __aiter__(self):
            for chunk in self:
                yield chunk

        async def aclose(self):
            self.close()

    def upstream(request):
        seen.append(request)
        assert request.method == method
        assert request.headers["Range"] == requested_range
        assert request.headers["If-Range"] == '"fixture-etag"'
        assert request.headers["Referer"] == "https://drive.example"
        assert request.headers["Accept-Encoding"] == "identity"
        headers = {
            "Content-Length": str(length),
            "Accept-Ranges": "bytes", "ETag": '"fixture-etag"',
            "Last-Modified": "Wed, 28 Oct 2026 00:00:00 GMT",
        }
        if status != 200:
            headers["Content-Range"] = f"bytes 100-199/{size}" if status == 206 else f"bytes */{size}"
        return httpx.Response(status, stream=Stream(), headers=headers)

    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    bridge = app.state.playback
    bridge.client.close()
    bridge.client = httpx.Client(transport=httpx.MockTransport(upstream))
    bridge.media_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    key = bridge._put(MediaAsset(
        None, "https://media.example/large.mkv", {"Referer": "https://drive.example"},
        "video/x-matroska", "fixture-trace", time.monotonic(),
    ), ".mkv")
    with TestClient(app) as client:
        response = client.request(method, "/t4/media/" + key, headers={
            "Range": requested_range, "If-Range": '"fixture-etag"',
        })
        assert response.status_code == status
        assert response.headers["ETag"] == '"fixture-etag"'
        assert response.headers["Last-Modified"] == "Wed, 28 Oct 2026 00:00:00 GMT"
        assert response.headers["Accept-Ranges"] == "bytes"
        assert response.headers["Content-Length"] == str(length)
        if status == 416:
            assert response.headers["Content-Range"] == f"bytes */{size}"
        elif status == 206:
            assert response.headers["Content-Range"] == f"bytes 100-199/{size}"
        else:
            assert "Content-Range" not in response.headers  # Never invent it for a 200.
        assert response.content == (b"" if method == "HEAD" or status == 416 else content)
    assert len(seen) == 1
    assert closed


def test_proxy_does_not_invent_range_metadata_missing_from_upstream(tmp_path):
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    bridge = app.state.playback
    bridge.client.close()
    transport = httpx.MockTransport(
        lambda _: httpx.Response(206, stream=httpx.ByteStream(b"fixture"))
    )
    bridge.client = httpx.Client(transport=transport)
    bridge.media_client = httpx.AsyncClient(transport=transport)
    key = bridge._put(MediaAsset(
        None, "https://media.example/file", {}, "application/octet-stream",
        "fixture-trace", time.monotonic(),
    ), ".mp4")
    with TestClient(app) as client:
        response = client.get("/t4/media/" + key, headers={"Range": "bytes=0-6"})
        assert response.status_code == 206
        assert "Content-Range" not in response.headers
        assert "Accept-Ranges" not in response.headers
