import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.at.subscription import SubscriptionParseError
from sidecar.at.native import NativeAtClient, NativeAtInvoker
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.adapter import T4Adapter
from tests.t4.test_adapter import FakeWorker
from tests.unit.test_fm import FakeAdapter
from tests.unit.test_uploads import JAVASCRIPT_PLUGIN, PYTHON_PLUGIN


def source(source_id="env-at"):
    return SourceConfig(
        source_id, "AT", subscription_url="https://at.example/sub/private-sub/1"
    )


async def fetcher(_source):
    return {"sites": [
        {
            "key": "bili", "name": "BiliBili", "api": "csp_BiliBili",
            "searchable": 0, "quickSearch": 0, "filterable": 1,
            "categories": ["Films"],
            "header": {"Authorization": "private-header"},
            "ext": {"token": "private-token", "secret": "private-secret"},
        },
        {"key": "push", "api": "csp_Push"},
        {"key": "web", "api": "csp_WebHome", "homePage": "https://at.example/private"},
        {
            "key": "raw", "name": "Python", "api": "csp_PyProxy",
            "ext": {
                "source": "https://at.example/plugins/private.py",
                "loader": "https://at.example/Atvp.py",
                "raw": True,
                "token": "private-token",
            },
        },
    ]}


def test_config_is_importable_and_does_not_expose_at_material(tmp_path):
    adapter = FakeAdapter()
    app = create_app(
        Settings(plugin_dir=str(tmp_path)),
        SourceRegistry((source(), source("second"))),
        subscription_fetcher=fetcher,
        t4_adapter=adapter,
    )
    with TestClient(app, base_url="http://nas.test:8765") as client:
        assert client.get("/t4/api/client-config").json() == {
            "subscription_path": "/t4/config.json"
        }
        response = client.get("/t4/config.json")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        sites = response.json()["sites"]
        assert adapter.requests == []
        assert len(sites) == 4
        assert len({site["key"] for site in sites}) == 4
        assert [site["name"] for site in sites] == ["BiliBili", "Python"] * 2
        assert sites[0]["categories"] == ["Films"]
        assert sites[0]["searchable"] == sites[0]["quickSearch"] == 0
        assert all(site["type"] == 4 for site in sites)
        for site in sites:
            assert site["api"].startswith("http://nas.test:8765/t4/fm/")
            assert client.get(site["api"], params={"filter": "true"}).status_code == 200
        assert "private" not in response.text
        assert "Authorization" not in response.text
        assert "ext" not in sites[0]


def test_subscription_has_fixed_address_and_no_signed_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("T4_GATEWAY_TOKEN", "obsolete-gateway")
    monkeypatch.setenv("T4_PLUGIN_DIR", str(tmp_path))
    adapter = FakeAdapter()
    app = create_app(
        Settings.from_env(), SourceRegistry((source(),)),
        subscription_fetcher=fetcher, t4_adapter=adapter,
    )
    with TestClient(app) as client:
        path = client.get("/t4/api/client-config").json()["subscription_path"]
        assert path == "/t4/config.json"
        response = client.get(path)
        assert response.status_code == 200
        assert "obsolete-gateway" not in response.text
        assert "private" not in response.text
        site = response.json()["sites"][0]
        assert site["api"] == "http://testserver/t4/fm/env-at/bili/"
        assert client.get(site["api"]).status_code == 200
        assert client.get("/t4/config/obsolete-key.json").status_code == 404
        assert client.get("/t4/fm/env-at/bili/obsolete-key/").status_code == 404


def test_unavailable_source_is_explicit_and_does_not_leak_subscription(tmp_path):
    async def failed(_source):
        raise SubscriptionParseError("failure with private-sub")

    app = create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=failed, t4_adapter=FakeAdapter(),
    )
    response = TestClient(app).get("/t4/config.json")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "subscription_unavailable"
    assert "private-sub" not in response.text


@pytest.mark.parametrize("language,content", [
    ("py", PYTHON_PLUGIN), ("js", JAVASCRIPT_PLUGIN),
])
@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_import_then_full_client_parameter_chain(tmp_path, language, content, transport):
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with TestClient(app) as client:
        registered = client.post(
            "/t4/api/plugins/upload", files={"file": (f"demo.{language}", content)}
        )
        assert registered.status_code == 200
        config = client.get("/t4/config.json")
        assert config.status_code == 200
        site = config.json()["sites"][0]
        api = site["api"]

        def call(params):
            if transport == "GET":
                response = client.get(api, params=params)
            elif transport == "JSON":
                response = client.post(api, json=params)
            else:
                response = client.post(api, data=params)
            assert response.status_code == 200, response.text
            return response.json()

        home = call({"filter": "true"})
        detail_id = home["list"][0]["vod_id"]
        assert detail_id.startswith("t4_")
        filters = base64.urlsafe_b64encode(b'{"sort":"time"}').decode().rstrip("=")
        category = call({"ac": "detail", "t": "all", "pg": "2", "ext": filters})
        assert category["page"] == 2
        search = call({"wd": "demo", "quick": "false", "pg": "3", "extend": ""})
        assert search["page"] == 3
        detail = call({"ac": "detail", "ids": detail_id})
        play_id = detail["list"][0]["vod_play_url"].split("$", 1)[1]
        assert play_id.startswith("t4_")
        play = call({"play": play_id, "flag": "Uploaded"})
        assert play["parse"] == 0
        assert play["url"].endswith(".m3u8")
        assert play["header"] == {}
        assert call({"ac": "action", "action": "refresh"})["action"] == "refresh"


def test_config_tracks_management_and_stable_keys(tmp_path):
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with TestClient(app) as client:
        assert client.get("/t4/config.json").json() == {"sites": []}
        first = client.post(
            "/t4/api/plugins/upload", files={"file": ("one.py", PYTHON_PLUGIN)}
        ).json()["plugin"]
        second = client.post(
            "/t4/api/plugins/upload", files={"file": ("two.py", PYTHON_PLUGIN + b"\n")}
        ).json()["plugin"]
        before = client.get("/t4/config.json").json()["sites"]
        assert len(before) == 2
        client.patch(f"/t4/api/plugins/{first['id']}", json={"name": "Renamed"})
        client.post("/t4/api/plugins/reorder", json={"plugin_ids": [second["id"], first["id"]]})
        after = client.get("/t4/config.json").json()["sites"]
        assert after[1]["name"] == "Renamed"
        assert after[1]["key"] == before[0]["key"]
        assert after[1]["api"] == before[0]["api"]
        client.delete(f"/t4/api/plugins/{first['id']}")
        remaining = client.get("/t4/config.json").json()["sites"]
        assert len(remaining) == 1
        assert remaining[0]["key"] == before[1]["key"]


def test_refresh_updates_unified_config_from_shared_snapshot(tmp_path):
    version = 1
    calls = 0

    async def changing(_source):
        nonlocal calls
        calls += 1
        return {"sites": [{"key": "bili", "api": "csp_BiliBili", "name": f"v{version}"}]}

    app = create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=changing, t4_adapter=FakeAdapter(),
    )
    with TestClient(app) as client:
        initial = client.get("/t4/config.json").json()["sites"][0]
        version = 2
        assert client.get("/t4/config.json").json()["sites"][0]["name"] == "v1"
        assert calls == 1
        assert client.get("/t4/api/sources/env-at/subscription?refresh=true").status_code == 200
        current = client.get("/t4/config.json").json()["sites"][0]
        assert current["name"] == "v2"
        assert current["key"] == initial["key"]
        assert calls == 2


def test_client_filters_and_false_quick_reach_original_native_api(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"list": []})

    registry = SourceRegistry((source(),))
    native = NativeAtClient(transport=httpx.MockTransport(handler))
    adapter = T4Adapter(
        registry, FakeWorker(), subscription_fetcher=fetcher,
        native_invoker=NativeAtInvoker(native),
    )
    app = create_app(
        Settings(plugin_dir=str(tmp_path)), registry,
        subscription_fetcher=fetcher, t4_adapter=adapter,
    )
    try:
        with TestClient(app) as client:
            api = client.get("/t4/config.json").json()["sites"][0]["api"]
            filters = {"year": "2026", "region": "CN"}
            ext = base64.urlsafe_b64encode(json.dumps(filters).encode()).decode().rstrip("=")
            category = client.get(
                api, params={"ac": "detail", "t": "all", "pg": "2", "ext": ext}
            )
            assert category.status_code == 200
            assert seen[-1].url.path == "/bilibili/private-token"
            assert seen[-1].url.params["filter.year"] == "2026"
            assert seen[-1].url.params["filter.region"] == "CN"
            assert seen[-1].url.params["pg"] == "2"
            search = client.post(api, data={"wd": "demo", "quick": "false"})
            assert search.status_code == 200
            assert seen[-1].url.params["quick"] == "false"
    finally:
        native.close()
