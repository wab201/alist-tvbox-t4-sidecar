import asyncio
import base64
import json
import time

from fastapi.testclient import TestClient
import httpx
from lxml import etree
import pytest

from sidecar.diagnostics import TraceStore, record, trace_scope
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.playback import PlaybackBridge, PlaybackError
from sidecar.t4.adapter import T4Adapter
from tests.t4.test_adapter import FakeWorker, source, fetcher
from tests.unit.test_uploads import PYTHON_PLUGIN


MPD = b"""<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">
<Period><AdaptationSet><Representation mimeType="video/mp4" codecs="avc1.640033">
<BaseURL>https://cdn.example/video.m4s?sign=private-signature</BaseURL>
<SegmentBase indexRange="100-200"><Initialization range="0-99"/></SegmentBase>
</Representation></AdaptationSet>
<AdaptationSet><Representation mimeType="audio/mp4" codecs="mp4a.40.2">
<BaseURL>https://cdn.example/audio.m4s?sign=private-signature</BaseURL>
<SegmentBase indexRange="100-200"><Initialization range="0-99"/></SegmentBase>
</Representation></AdaptationSet></Period></MPD>"""


def dash_payload():
    return {
        "url": "data:application/dash+xml;base64," + base64.b64encode(MPD).decode(),
        "parse": "0", "jx": "0",
        "header": {"Cookie": "private-cookie", "Referer": "https://www.bilibili.com"},
        "subs": [{"name": "Chinese", "url": "https://subs.example/1.srt"}],
    }


class DashWorker(FakeWorker):
    def call(self, runtime_key, plugin, method, params):
        if method == "play":
            return dash_payload()
        return super().call(runtime_key, plugin, method, params)


def application(tmp_path, worker=None):
    registry = SourceRegistry((source(),))
    adapter = T4Adapter(registry, worker or DashWorker(), subscription_fetcher=fetcher)
    return create_app(
        Settings(plugin_dir=str(tmp_path), log_file=str(tmp_path / "logs.jsonl")),
        registry, subscription_fetcher=fetcher, t4_adapter=adapter,
    )


@pytest.mark.parametrize("entry,transport", [
    ("fm", "GET"), ("fm", "JSON"), ("fm", "FORM"),
    ("module", "GET"), ("module", "JSON"),
])
def test_inline_dash_http_and_range_proxy_keep_audio_headers_and_parent_trace(
    tmp_path, entry, transport,
):
    app = application(tmp_path)
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.headers["cookie"] == "private-cookie"
        assert request.headers["range"] == "bytes=0-4095"
        return httpx.Response(206, stream=httpx.ByteStream(b"\x00\x00\x00\x20ftyp" + bytes(4088)), headers={
            "Content-Range": "bytes 0-4095/100000", "Accept-Ranges": "bytes",
        })

    app.state.playback.client.close()
    app.state.playback.client = httpx.Client(transport=httpx.MockTransport(upstream))
    with TestClient(app, base_url="https://public.example:9443") as client:
        path = "/t4/fm/source-a/demo/" if entry == "fm" else "/t4/api/play"
        params = {"play": "raw", "source": "source-a", "site": "demo"}
        if transport == "GET":
            response = client.get(path, params=params)
        elif transport == "JSON":
            response = client.post(path, json=params)
        else:
            response = client.post(path, data=params)
        assert response.status_code == 200
        play_trace = response.headers["x-t4-trace-id"]
        payload = response.json()
        assert payload["url"].startswith("https://public.example:9443/t4/media/")
        assert payload["parse"] == payload["jx"] == 0
        assert payload["header"] == {}
        assert payload["subs"] == dash_payload()["subs"]
        manifest = client.get(payload["url"])
        assert manifest.status_code == 200
        assert "private-signature" not in manifest.text
        assert client.head(payload["url"]).headers["content-length"] == str(len(manifest.content))
        root = etree.fromstring(manifest.content)
        for node in root.findall(".//{*}BaseURL"):
            media = client.get(node.text, headers={"Range": "bytes=0-4095"})
            assert media.status_code == 206
            assert media.content[4:8] == b"ftyp"
            assert media.headers["content-range"] == "bytes 0-4095/100000"
            assert client.head(node.text, headers={"Range": "bytes=0-4095"}).status_code == 206
        events = client.get("/t4/api/diagnostics/events", params={"trace": play_trace}).json()["events"]
        assert any(event["stage"] == "media.upstream" for event in events)
        assert len({event["trace_id"] for event in events}) >= 4
        exported = client.get("/t4/api/diagnostics/export").text
        for secret in ("private-cookie", "private-signature", "cdn.example", "Cookie", "data:"):
            assert secret not in exported
        assert client.get("/t4/media/missing.mpd").status_code == 410
    assert len(seen) == 4
    assert "private-cookie" not in (tmp_path / "logs.jsonl").read_text()


@pytest.mark.parametrize("content", [
    "data:application/dash+xml;base64,broken!",
    "data:application/dash+xml;base64," + base64.b64encode(b"<html/>").decode(),
    "data:application/dash+xml;base64," + base64.b64encode(
        MPD.replace(b"https://cdn.example/video.m4s?sign=private-signature", b"file:///etc/passwd")
    ).decode(),
])
def test_bad_inline_dash_is_not_exposed_as_a_playable_url(content):
    bridge = PlaybackBridge()
    try:
        with pytest.raises(PlaybackError):
            bridge.prepare({"url": content}, "https://public.example")
    finally:
        bridge.close()


def test_diagnostics_checks_audio_and_video_without_claiming_device_success(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        return httpx.Response(206, content=b"\x00\x00\x00\x20ftyp" + bytes(4088))

    class SearchDash(DashWorker):
        def call(self, runtime_key, plugin, method, params):
            if method == "search":
                return {"list": [{"vod_id": "movie", "vod_name": "Demo"}]}
            if method == "detail":
                assert params["ac"] == "detail"
            return super().call(runtime_key, plugin, method, params)

    # Rebuild with a searchable fixture; no network or live plugin is changed.
    app = application(tmp_path, SearchDash())
    app.state.playback.client.close()
    app.state.playback.client = httpx.Client(transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        result = client.post("/t4/api/diagnostics/check", json={
            "source_id": "source-a", "plugin_id": "demo", "keyword": "Demo",
        }).json()
        assert result["ok"] is True
        assert result["real_device_verified"] is False
        assert {item["kind"] for item in result["tracks"]} == {"audio", "video"}
        assert calls == ["/video.m4s", "/audio.m4s"]
        exported = client.get("/t4/api/diagnostics/export", params={"trace": result["trace_id"]})
        assert "adapter.complete" in exported.text
        assert "private-cookie" not in exported.text


def test_trace_context_survives_to_thread_and_connects_multiple_requests():
    store = TraceStore(capacity=5)

    async def run():
        with trace_scope(store, trace_id="a", source="public"):
            await asyncio.to_thread(record, "at.response", status=206, url="secret-url")
        with trace_scope(store, trace_id="b"):
            record("id.resolved", parent_trace="a")
        with trace_scope(store, trace_id="c"):
            record("id.resolved", parent_trace="b")
            record("media.closed", bytes=4096)

    asyncio.run(run())
    assert {event["trace_id"] for event in store.events(trace_id="a")} == {"a", "b", "c"}
    assert "secret-url" not in json.dumps(store.events())
    assert all("url" not in event for event in store.events())


def test_worker_stderr_is_drained_but_raw_text_never_enters_logs():
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        app = create_app(Settings(plugin_dir=directory), SourceRegistry())
        plugin = PYTHON_PLUGIN.replace(
            b'def homeContent(self, filter):',
            b'def homeContent(self, filter):\n        print("Cookie=private-cookie https://private.example/sub/token")',
        )
        with TestClient(app) as client:
            uploaded = client.post("/t4/api/plugins/upload", files={"file": ("logs.py", plugin)})
            api = uploaded.json()["plugin"]["fm_path"]
            response = client.get(api)
            assert response.status_code == 200
            for _ in range(20):
                events = app.state.traces.events(trace_id=response.headers["x-t4-trace-id"])
                if any(event["stage"] == "worker.stderr" for event in events):
                    break
                time.sleep(.02)
            assert any(event["stage"] == "worker.stderr" for event in events)
            assert "private-cookie" not in json.dumps(events)
            assert "private.example" not in json.dumps(events)
            assert any(event["stage"] == "worker.complete" for event in events)


def test_trace_store_has_bounded_memory_and_rotates_files(tmp_path):
    store = TraceStore(str(tmp_path / "trace.jsonl"), capacity=3)
    store._handler.maxBytes = 150
    for index in range(8):
        store.emit("test", trace_id="safe", status=index, Cookie="hidden")
    assert len(store.events()) == 3
    assert store.events(after=7)[0]["status"] == 7
    store.close()
    assert len(list(tmp_path.glob("trace.jsonl*"))) == 3
    assert "hidden" not in (tmp_path / "trace.jsonl").read_text()
    restored = TraceStore(str(tmp_path / "trace.jsonl"), capacity=3)
    assert len(restored.events()) == 3
    restored.emit("restart", trace_id="safe")
    assert restored.events()[-1]["seq"] == 9
    restored.close()


def test_manifest_only_is_not_misreported_as_verified_media():
    bridge = PlaybackBridge(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"#EXTM3U\n#EXTINF:5,\nsegment.ts\n")
    ))
    try:
        result = bridge.probe({"url": "https://video.example/master.m3u8"})
        assert result["ok"] is False
        assert result["reason"] == "manifest_only"
        assert result["real_device_verified"] is False
    finally:
        bridge.close()
