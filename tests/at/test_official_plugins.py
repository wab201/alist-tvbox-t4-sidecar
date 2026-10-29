import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.at.native import NativeAtClient, NativeAtInvoker
from sidecar.at.subscription import SubscriptionParser
from sidecar.gateway import main
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.adapter import T4Adapter
from tests.at.test_official_t4 import AT, TOKEN, NoWorker, official_site
from tests.unit.test_compatibility import MP4, PNG


def remote_site(key="plugin-42", *, token=TOKEN):
    return {
        "key": key, "name": "Remote fixture", "type": 4,
        "api": f"{AT}/t4/{token}/v/{key}",
        "searchable": 1, "quickSearch": 0, "filterable": 1,
        "categories": ["movies"], "timeout": 45,
    }


@pytest.mark.parametrize("key", [
    "plugin-42", "9fe813c240", "csp_CustomPlugin", "custom_source", "Unknown",
])
@pytest.mark.parametrize("site_type", [4, "4"])
def test_remote_descriptor_preserves_external_key_and_assigns_http_owner(key, site_type):
    site = {**remote_site(key), "type": site_type}
    snapshot = SubscriptionParser(AT).parse("at", {"sites": [site]})
    assert snapshot.errors == ()
    plugin = snapshot.plugin_for(key)
    assert plugin.plugin_id == plugin.api == key
    assert plugin.plugin_type == "official_t4"
    assert plugin.t4_endpoint == site["api"]
    assert plugin.source == plugin.loader == ""
    metadata = plugin.public_metadata()
    assert metadata["transport"] == "official_t4"
    assert metadata["compatibility_mode"] == "official_remote_plugin"
    assert metadata["execution_owner"] == "at"
    assert metadata["capabilities"] == ["home", "category", "search", "detail", "play"]
    assert "native_route" not in metadata
    assert plugin.site_options["quickSearch"] == 0
    assert TOKEN not in repr(plugin)
    assert TOKEN not in json.dumps(snapshot.public_metadata())


def test_remote_descriptor_does_not_interpret_client_ext_or_download_code():
    site = {
        **remote_site(),
        # Execution configuration belongs to AT, not to the HTTP consumer.
        "ext": "opaque-remote-configuration",
        "jar": AT + "/unused.jar",
    }
    snapshot = SubscriptionParser(AT).parse("at", {"sites": [site]})
    assert snapshot.errors == ()
    assert snapshot.plugins[0].source == snapshot.plugins[0].loader == ""
    assert "opaque-remote" not in json.dumps(snapshot.public_metadata())


@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
@pytest.mark.parametrize("entry", ["fm", "fm_module", "module"])
def test_remote_plugin_full_http_contract(tmp_path, transport, entry, monkeypatch):
    seen = []
    media = "https://media.example/video.mkv?signature=fixture"
    cover = "https://image.example/poster.jpg"
    headers = {"Referer": "https://drive.example", "User-Agent": "fixture"}
    filters = {"genre": "科幻", "sort": "new"}

    def backend(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/t4/{TOKEN}/v/plugin-42"
        query = request.url.params
        if "play" in query:
            assert dict(query) == {"play": "episode-1", "flag": "原画"}
            return httpx.Response(200, json={
                "parse": "0", "jx": "0", "url": media,
                "header": json.dumps(headers), "type": "PAN115",
                "subs": [{"name": "中文", "url": "https://sub.example/1.srt"}],
                "audio_url": "https://media.example/audio.m4a",
                "chapters": [{"name": "片头", "start": 0}],
            })
        item = {"vod_id": "item-1", "vod_name": "Fixture", "vod_pic": cover}
        if "ids" in query:
            assert dict(query) == {"ac": "detail", "ids": "item-1"}
            item.update(vod_play_from="原画", vod_play_url="第一集$episode-1")
        if "t" in query:
            assert query["t"] == "movies" and query["pg"] == "2"
            raw = query["ext"]
            assert json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))) == filters
        if "wd" in query:
            # Verify transmission only: 1.121.0's controller drops pg before its Worker.
            assert dict(query) == {"wd": "Fixture", "quick": "false", "pg": "2"}
        return httpx.Response(200, json={
            "class": [{"type_id": "movies", "type_name": "电影"}],
            "filters": {"movies": [{"key": "genre", "value": []}]},
            "list": [item], "page": int(query.get("pg", "1")),
        })

    native = NativeAtClient(transport=httpx.MockTransport(backend))
    monkeypatch.setattr(main, "NativeAtClient", lambda **_kwargs: native)
    monkeypatch.setattr(main.AtvpWorkerInvoker, "call", NoWorker().call)

    async def fetch(_):
        return {"sites": [remote_site()]}

    registry = SourceRegistry((SourceConfig("at", "AT", subscription_url=AT + "/t4/-/1"),))
    with TestClient(create_app(
        Settings(plugin_dir=str(tmp_path)), registry, fetch,
    )) as client:
        site = client.get("/t4/config.json").json()["sites"][0]
        assert site["type"] == 4 and site["quickSearch"] == 0
        assert site["categories"] == ["movies"] and site["timeout"] == 45
        assert site["api"].endswith("/t4/fm/at/plugin-42/")
        assert "ext" not in site and "jar" not in site
        metadata = client.get("/t4/api/sources/at/subscription")
        assert metadata.json()["errors"] == []
        assert metadata.json()["plugins"][0]["fm_path"].endswith("/plugin-42/")
        assert TOKEN not in metadata.text

        def call(module, params):
            path = site["api"]
            if entry == "fm_module":
                path += "api/" + module
            elif entry == "module":
                path = "/t4/api/" + module
                params = {**params, "source": "at", "site": "plugin-42"}
            if transport == "GET":
                return client.get(path, params=params)
            if transport == "JSON":
                return client.post(path, json=params)
            return client.post(path, data=params)

        home = call("home", {})
        assert home.status_code == 200, home.text
        assert home.json()["class"][0]["type_id"] == "movies"
        assert home.json()["filters"]["movies"][0]["key"] == "genre"
        encoded = base64.urlsafe_b64encode(json.dumps(filters).encode()).decode().rstrip("=")
        category = call("category", {
            "t": "movies", "pg": 2,
            "ext" if entry == "fm" else "extend": encoded if entry == "fm" else json.dumps(filters),
        })
        assert category.status_code == 200 and category.json()["page"] == 2
        search = call("search", {"wd": "Fixture", "quick": False, "pg": 2})
        assert search.status_code == 200
        detail = call("detail", {"ac": "detail", "ids": home.json()["list"][0]["vod_id"]})
        assert detail.status_code == 200
        vod = detail.json()["list"][0]
        assert vod["vod_pic"] == cover
        play_id = vod["vod_play_url"].partition("$")[2]
        assert play_id.startswith("t4_") and play_id != "episode-1"
        play = call("play", {"play": play_id, "flag": "原画"})
        assert play.status_code == 200, play.text
        payload = play.json()
        assert payload["url"] == media and payload["header"] == headers
        assert payload["parse"] == payload["jx"] == 0
        assert payload["type"] == "PAN115"
        assert payload["subs"][0]["name"] == "中文"
        assert payload["audio_url"].endswith(".m4a") and payload["chapters"]
        count = len(seen)
        action = call("action", {"ac": "action", "id": "do-not-run", "action": "delete"})
        assert action.status_code == 501
        assert len(seen) == count
        events = client.get("/t4/api/diagnostics/events").json()["events"]
        assert any(event.get("backend") == "official_t4" for event in events)
        assert TOKEN not in json.dumps(events)
    assert native._client.is_closed


@pytest.mark.parametrize("failure", ["status", "json", "shape", "transport"])
def test_remote_failures_do_not_fall_back_to_local_worker(tmp_path, failure, monkeypatch):
    seen = []

    def backend(request):
        seen.append(request)
        if failure == "transport":
            raise httpx.ConnectError(TOKEN, request=request)
        if failure == "status":
            return httpx.Response(404, text=TOKEN)
        if failure == "shape":
            return httpx.Response(200, json=[TOKEN])
        return httpx.Response(200, text=TOKEN)

    native = NativeAtClient(transport=httpx.MockTransport(backend))
    monkeypatch.setattr(main, "NativeAtClient", lambda **_kwargs: native)
    monkeypatch.setattr(main.AtvpWorkerInvoker, "call", NoWorker().call)

    async def fetch(_):
        return {"sites": [remote_site()]}

    registry = SourceRegistry((SourceConfig("at", "AT", subscription_url=AT + "/t4/-/1"),))
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), registry, fetch)) as client:
        response = client.get("/t4/fm/at/plugin-42/")
        assert response.status_code == 502
        assert len(seen) == 1
        assert TOKEN not in response.text
        assert TOKEN not in json.dumps(client.get("/t4/api/diagnostics/events").json())


def test_remote_refresh_uses_rotated_endpoint_but_keeps_site_and_play_context(tmp_path, monkeypatch):
    revision = "first"
    seen = []

    def backend(request):
        seen.append(request.url.path)
        if "play" in request.url.params:
            assert request.url.params["play"] == "episode-1"
            return httpx.Response(200, json={"url": "https://media.example/video.mp4"})
        return httpx.Response(200, json={"list": [{
            "vod_id": "item-1", "vod_name": "Fixture",
            "vod_play_from": "正片", "vod_play_url": "第一集$episode-1",
        }]})

    native = NativeAtClient(transport=httpx.MockTransport(backend))
    monkeypatch.setattr(main, "NativeAtClient", lambda **_kwargs: native)
    monkeypatch.setattr(main.AtvpWorkerInvoker, "call", NoWorker().call)

    async def fetch(_):
        return {"sites": [remote_site(token=revision)]}

    registry = SourceRegistry((SourceConfig("at", "AT", subscription_url=AT + "/t4/-/1"),))
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), registry, fetch)) as client:
        original = client.get("/t4/config.json").json()["sites"][0]
        detail = client.get(original["api"], params={"ids": "item-1"}).json()
        play_id = detail["list"][0]["vod_play_url"].partition("$")[2]
        revision = "second"
        metadata = client.get("/t4/api/sources/at/subscription?refresh=true")
        assert metadata.json()["errors"] == []
        current = client.get("/t4/config.json").json()["sites"][0]
        assert current == original
        assert client.get(current["api"], params={"play": play_id}).status_code == 200
    assert seen == ["/t4/first/v/plugin-42", "/t4/second/v/plugin-42"]


def test_remote_gate_uses_http_and_does_not_reset_a_sidecar_worker(tmp_path, monkeypatch):
    seen = []

    def backend(request):
        seen.append(request)
        query = request.url.params
        if "play" in query:
            assert query["play"] == "episode-1"
            return httpx.Response(200, json={"url": "https://media.example/video.mp4"})
        item = {"vod_id": "item-1", "vod_name": "Fixture", "vod_pic": "https://media.example/cover"}
        if "ids" in query:
            item.update(vod_play_from="正片$$$操作", vod_play_url="第一集$episode-1$$$删除$delete")
        return httpx.Response(200, json={
            "class": [{"type_id": "movies", "type_name": "电影"}], "list": [item],
        })

    native = NativeAtClient(transport=httpx.MockTransport(backend))
    monkeypatch.setattr(main, "NativeAtClient", lambda **_kwargs: native)
    monkeypatch.setattr(main.AtvpWorkerInvoker, "call", NoWorker().call)

    def no_reset(*_):
        raise AssertionError("an AT-owned Worker must not be reset by Sidecar")

    monkeypatch.setattr(main.T4Adapter, "reset_plugin", no_reset)

    async def fetch(_):
        return {"sites": [remote_site()]}

    registry = SourceRegistry((SourceConfig("at", "AT", subscription_url=AT + "/t4/-/1"),))
    report_path = tmp_path / "gate.json"
    app = create_app(Settings(plugin_dir=str(tmp_path), compatibility_report=str(report_path)), registry, fetch)

    def resources(request):
        if request.url.path == "/cover":
            return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})
        start, end = map(int, request.headers["range"][6:].split("-"))
        end = min(end, len(MP4) - 1)
        return httpx.Response(206, content=MP4[start:end + 1], headers={
            "Content-Range": f"bytes {start}-{end}/{len(MP4)}", "Content-Type": "video/mp4",
        })

    gate = app.state.compatibility
    gate.probes.client.close()
    gate.probes.client = httpx.Client(transport=httpx.MockTransport(resources))
    with TestClient(app) as client:
        assert client.get("/t4/fm/at/plugin-42/").status_code == 200
        assert client.post("/t4/api/compatibility/runs", json={}).status_code == 202

        async def finish():
            await gate.task
            return gate.report

        report = client.portal.call(finish)
        assert report["state"] == "completed" and report["gate"]["passed"] is True
        assert report["plugins"][0]["classification"] == "official_remote_plugin"
        assert report["plugins"][0]["status"] == "pass"
        assert all(stage["status"] == "pass" for stage in report["plugins"][0]["stages"].values())
        assert report["real_device_verified"] is report["long_play_verified"] is False
        assert report["remote_plugin_source_verified"] is False
        assert all(request.url.path == f"/t4/{TOKEN}/v/plugin-42" for request in seen)
        assert TOKEN not in report_path.read_text(encoding="utf-8")


def test_remote_raw_encrypted_and_builtin_sources_keep_distinct_owners(tmp_path):
    calls = []

    class Worker:
        def call(self, runtime_key, plugin, method, params):
            calls.append((plugin.plugin_id, plugin.plugin_type, method))
            return {"list": []}

    sites = [
        remote_site(), official_site(),
        {"key": "raw", "api": "csp_PyProxy", "type": 3,
         "ext": {"source": AT + "/plugin.py", "raw": True}},
        {"key": "encrypted", "api": "csp_PyProxy", "type": 3,
         "ext": {"source": AT + "/plugin.txt", "loader": AT + "/Atvp.py"}},
    ]

    async def fetch(_):
        return {"sites": sites}

    native_calls = []
    native = NativeAtClient(transport=httpx.MockTransport(
        lambda request: (native_calls.append(request) or httpx.Response(200, json={"list": []}))
    ))
    registry = SourceRegistry((SourceConfig("at", "AT", subscription_url=AT + "/t4/-/1"),))
    adapter = T4Adapter(registry, Worker(), subscription_fetcher=fetch, native_invoker=NativeAtInvoker(native))
    try:
        with TestClient(create_app(
            Settings(plugin_dir=str(tmp_path)), registry, fetch, t4_adapter=adapter,
        )) as client:
            exported = client.get("/t4/config.json").json()["sites"]
            assert len(exported) == 4
            for site in exported:
                assert client.get(site["api"]).status_code == 200
        assert calls == [("raw", "raw_python", "home"), ("encrypted", "secspider", "home")]
        assert [request.url.path.rsplit("/", 1)[-1] for request in native_calls] == ["plugin-42", "Media"]
    finally:
        native.close()
