import asyncio

from fastapi.testclient import TestClient
import httpx
import pytest

from sidecar.playback import PlaybackBridge
from sidecar.plugin_proxy import PluginProxyRegistry
from tests.unit.test_playback_diagnostics import FakeWorker, application


PLAYER_HLS = "application/x-mpegURL"
HTTP_HLS = "application/vnd.apple.mpegurl"
HLS_FORMATS = [
    PLAYER_HLS, "application/x-mpegurl", HTTP_HLS,
    " Application/Vnd.Apple.MpegURL ; charset=UTF-8 ",
    " APPLICATION/X-MPEGURL; charset=UTF-8",
]


@pytest.fixture
def bridge():
    instance = PlaybackBridge(proxy_registry=PluginProxyRegistry())
    yield instance
    asyncio.run(instance.aclose())
    instance.close()


@pytest.mark.parametrize("media_format", HLS_FORMATS)
@pytest.mark.parametrize("path", ["index.M3U8?sign=fixture", "watch?id=fixture"])
def test_hls_player_mime_selects_hls_without_changing_http_mime(bridge, media_format, path):
    original = {
        "url": "https://cdn.example/" + path, "format": media_format,
        "header": {"Referer": "https://allowed.example", "User-Agent": "fixture-agent"},
        "parse": 0, "position": 42, "subs": [],
    }
    result = bridge.prepare(original, "https://public.example/prefix")
    # FongMi passes format directly to MediaItem.setMimeType. Media3's explicit
    # HLS source selector expects APPLICATION_M3U8, not the HTTP apple MIME.
    assert result["format"] == PLAYER_HLS
    asset = bridge.get(result["url"].rsplit("/", 1)[-1])
    assert asset.kind == "hls" and asset.mime == HTTP_HLS
    assert asset.url == original["url"] and asset.headers == original["header"]
    assert result["header"] == {} and result["position"] == 42 and result["subs"] == []
    assert original["format"] == media_format
    assert bridge.prepare(result, "https://public.example/prefix") == result
    assert bridge._size == 1


@pytest.mark.parametrize("stale_format", [None, HTTP_HLS, "application/octet-stream"])
def test_existing_hls_asset_restores_player_hint_without_another_relay(bridge, stale_format):
    result = bridge.prepare({"url": "https://cdn.example/index.m3u8"}, "https://public.example")
    if stale_format is None:
        result.pop("format")
    else:
        result["format"] = stale_format
    repaired = bridge.prepare(result, "https://public.example")
    assert repaired["format"] == PLAYER_HLS
    assert repaired["url"] == result["url"] and bridge._size == 1


@pytest.mark.parametrize("media_format", HLS_FORMATS)
def test_existing_worker_hls_proxy_normalizes_hint_without_wrapping(bridge, media_format):
    key = bridge._proxies.register("http://127.0.0.1:5000/proxy", "fixture", "fixture")
    original = {
        "url": "https://public.example/t4/proxy/" + key + "?do=media",
        "format": media_format, "header": {},
    }
    result = bridge.prepare(original, "https://public.example")
    assert result["format"] == PLAYER_HLS
    assert result["url"] == original["url"] and bridge._size == 0
    assert original["format"] == media_format


@pytest.mark.parametrize("path,media_format", [
    ("episode.m3u8", None), ("episode", PLAYER_HLS), ("episode", HTTP_HLS),
])
def test_at_relay_keeps_network_mime_separate_from_player_mime(bridge, path, media_format):
    original = {"url": "https://at.example/p/fixture-token/" + path, "header": {}}
    if media_format:
        original["format"] = media_format
    result = bridge.relay_at(
        original, "https://public.example", backend_api="https://at.example",
        token="fixture-token", internal_url="http://at.internal:4567",
    )
    assert result["format"] == PLAYER_HLS and result["url"].endswith(".m3u8")
    asset = bridge.get(result["url"].rsplit("/", 1)[-1])
    assert asset.kind == "hls" and asset.mime == HTTP_HLS and asset.local_transport
    assert not asset.redirectable
    assert bridge.prepare(result, "https://public.example") == result
    assert bridge._size == 1


@pytest.mark.parametrize("media_format,path", [
    ("video/mp4", "video.mp4"), ("application/dash+xml", "watch"),
    ("video/x-matroska", "video.mkv"), (None, "watch"),
])
def test_non_hls_formats_remain_unchanged(bridge, media_format, path):
    original = {"url": "https://cdn.example/" + path, "header": {"User-Agent": "fixture"}}
    if media_format:
        original["format"] = media_format
    assert bridge.prepare(original, "https://public.example") == original
    assert bridge._size == 0


@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_t4_http_player_contract_uses_media3_hls_hint(tmp_path, transport):
    class Worker(FakeWorker):
        def call(self, runtime_key, plugin, method, params):
            if method == "play":
                return {"url": "https://cdn.example/watch", "format": HTTP_HLS, "parse": 0}
            return super().call(runtime_key, plugin, method, params)

    app = application(tmp_path, Worker())
    with TestClient(app) as client:
        path, params = "/t4/fm/source-a/demo/", {"play": "fixture"}
        if transport == "GET":
            response = client.get(path, params=params)
        elif transport == "JSON":
            response = client.post(path, json=params)
        else:
            response = client.post(path, data=params)
        assert response.status_code == 200
        result = response.json()
        assert result["format"] == PLAYER_HLS
        assert result["parse"] == result["jx"] == 0 and result["header"] == {}
        asset = app.state.playback.get(result["url"].rsplit("/", 1)[-1])
        assert asset.kind == "hls" and asset.mime == HTTP_HLS
