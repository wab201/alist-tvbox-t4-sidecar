import base64
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from sidecar.playback import PlaybackBridge, PlaybackError
from sidecar.server_parser import extract_media
from tests.unit.test_playback_diagnostics import FakeWorker, application


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_maccms_declarative_player_is_parsed_without_running_javascript(mode):
    target = "https://media.example/video.m3u8?signature=private"
    value = target if mode in (0, 1) else base64.b64encode(target.encode()).decode()
    script = "var player_aaaa=" + json.dumps({"url": value, "encrypt": mode})
    assert extract_media(script.encode(), "https://page.example/play/1") == target


@pytest.mark.parametrize("markup", [
    b'<video src="/media/video.mp4"></video>',
    b'<video><source src="/media/video.mp4"/></video>',
])
def test_html_video_sources_are_resolved_against_page(markup):
    assert extract_media(markup, "https://page.example/play/1") == "https://page.example/media/video.mp4"


@pytest.mark.parametrize("markup", [
    b"<html>no media</html>",
    b'<script>window.eval("dangerous")</script>',
    b'<video src="javascript:evil.mp4"></video>',
    b'var player_aaaa={"url":"https://other.example/page.html","encrypt":0}',
    b'var player_aaaa={"url":"broken","encrypt":2}',
])
def test_unsupported_page_never_manufactures_direct_media(markup):
    assert extract_media(markup, "https://page.example/play/1") is None


def test_page_is_parsed_on_server_and_direct_media_headers_go_to_player(tmp_path):
    class PageWorker(FakeWorker):
        def call(self, runtime_key, plugin, method, params):
            if method == "play":
                return {"url": "https://page.example/1.html", "parse": 1, "jx": 0,
                        "header": {"Cookie": "private-cookie", "Authorization": "private-auth",
                                   "User-Agent": "test-agent"}}
            return super().call(runtime_key, plugin, method, params)
    seen = []
    def respond(request):
        seen.append(request)
        if request.url.host == "page.example":
            assert request.headers["cookie"] == "private-cookie"
            return httpx.Response(200, content=b'<video src="https://cdn.example/video.mp4"></video>')
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert request.headers["referer"] == "https://page.example/1.html"
        return httpx.Response(200, stream=httpx.ByteStream(b"\x00\x00\x00\x20ftyp" + bytes(100)))
    app = application(tmp_path, PageWorker())
    app.state.playback.client.close()
    app.state.playback.client = httpx.Client(transport=httpx.MockTransport(respond))
    with TestClient(app) as client:
        payload = client.get("/t4/fm/source-a/demo/", params={"play": "raw"}).json()
        assert payload["parse"] == payload["jx"] == 0 and payload["playUrl"] == ""
        assert payload["url"] == "https://cdn.example/video.mp4"
        assert payload["header"]["Referer"] == "https://page.example/1.html"
        assert "Cookie" not in payload["header"] and "Authorization" not in payload["header"]
        assert all(value not in client.get("/t4/api/diagnostics/export").text
                   for value in ("private-cookie", "private-auth", "page.example", "cdn.example"))
    assert len(seen) == 1


def test_unresolved_page_returns_explicit_502_not_a_client_sniffing_command(tmp_path):
    class PageWorker(FakeWorker):
        def call(self, runtime_key, plugin, method, params):
            if method == "play":
                return {"url": "https://page.example/1.html", "parse": 1}
            return super().call(runtime_key, plugin, method, params)
    app = application(tmp_path, PageWorker())
    app.state.playback.client.close()
    app.state.playback.client = httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"<html>unsupported player</html>")
    ))
    with TestClient(app) as client:
        for path in ("/t4/fm/source-a/demo/", "/t4/api/play"):
            response = client.get(path, params={"play": "raw", "source": "source-a", "site": "demo"})
            assert response.status_code == 502
            assert response.json()["detail"]["code"] == "server_parser_unresolved"
            assert "page.example" not in response.text


def test_parser_payload_limit_is_enforced_without_reading_unbounded_page():
    bridge = PlaybackBridge(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"x" * 1_100_000)
    ))
    try:
        with pytest.raises(PlaybackError, match="server_parser_limit"):
            bridge.prepare({"url": "https://page.example/", "parse": 1}, "http://public.example")
    finally:
        bridge.close()


def test_parse_redirect_to_extensionless_hls_stays_on_server():
    bridge = PlaybackBridge(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"#EXTM3U\n#EXTINF:5,\none.ts\n#EXT-X-ENDLIST\n")
    ))
    try:
        result = bridge.prepare({"url": "https://page.example/watch?id=1", "parse": 1},
                                "https://public.example")
        asset = bridge.get(result["url"].rsplit("/", 1)[-1])
        assert result["header"] == {} and result["parse"] == result["jx"] == 0
        assert asset.kind == "hls" and asset.url == "https://page.example/watch?id=1"
    finally:
        bridge.close()


def test_parser_transport_failure_does_not_expose_url_or_request_headers():
    def fail(request):
        raise httpx.ReadTimeout("https://private.example/?token=secret", request=request)
    bridge = PlaybackBridge(transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(PlaybackError, match="^server_parser_request_failed$"):
            bridge.prepare({"url": "https://page.example/", "parse": 1}, "https://public.example")
    finally:
        bridge.close()


def test_protected_direct_video_and_headers_are_preserved_for_player():
    bridge = PlaybackBridge()
    try:
        original = {
            "url": "https://cdn.example/video.mp4?sign=private", "parse": 0,
            "header": {"Cookie": "secret", "Referer": "https://page.example"},
        }
        result = bridge.prepare(original, "https://public.example")
        assert result == original
        assert bridge._size == 0
    finally:
        bridge.close()


def test_extensionless_video_keeps_original_format_and_headers():
    bridge = PlaybackBridge()
    try:
        original = {
            "url": "https://cdn.example/watch?id=1", "format": "video/mp4",
            "parse": 0, "header": {"Referer": "https://page.example"},
        }
        assert bridge.prepare(original, "https://public.example") == original
        assert bridge._size == 0
    finally:
        bridge.close()


@pytest.mark.parametrize("path", ["/t4/fm/source-a/demo/", "/t4/api/play"])
def test_worker_timeout_is_explicit_and_does_not_claim_client_parser_failure(tmp_path, path):
    from sidecar.worker.client import WorkerTimeoutError
    class TimeoutWorker(FakeWorker):
        def call(self, runtime_key, plugin, method, params):
            raise WorkerTimeoutError("private internal timeout")
    with TestClient(application(tmp_path, TimeoutWorker())) as client:
        response = client.get(path, params={"play": "raw", "source": "source-a", "site": "demo"})
        assert response.status_code == 504
        assert response.json()["detail"]["code"] == "worker_timeout"
        assert "private" not in response.text
