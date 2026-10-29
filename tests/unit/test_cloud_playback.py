import asyncio
import base64
from dataclasses import replace
import json
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import httpx
import pytest

from sidecar.at.native import NativeAtClient, NativeAtInvoker
from sidecar.at.subscription import PluginDescriptor, SubscriptionSnapshot
from sidecar.at.subscription import SubscriptionParser
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.playback import PlaybackBridge
from sidecar.t4.adapter import T4Adapter, T4Request


def plugin(**values):
    return PluginDescriptor(**{
        "plugin_id": "cloud", "name": "Cloud", "plugin_type": "builtin",
        "api": "csp_XiaoYa", "source": "", "loader": "", "raw": False,
        "token": "fixture-token", "secret": "",
        "local_proxy_config": {"PAN115": {"enable": True, "concurrency": 2}},
        "data": None, "filters": (), "backend_api": "https://at.example/at",
        **values,
    })


def test_cloud_settings_persist_without_changing_subscription(tmp_path):
    source = SourceConfig("demo", "Demo", subscription_url="https://at.example/sub/private/0")
    path = "/t4/api/sources/demo/playback"
    settings = Settings(plugin_dir=str(tmp_path / "plugins"))
    with TestClient(create_app(settings, SourceRegistry((source,)))) as client:
        assert client.get(path).json()["mode"] == "direct"
        response = client.put(path, json={
            "mode": "server", "at_internal_url": "http://at:4567",
        })
        assert response.status_code == 200
        assert response.json()["at_internal_url"] == "http://at:4567"
        assert "private" not in response.text and "token" not in response.text
    registry = SourceRegistry((source,))
    with TestClient(create_app(settings, registry)) as client:
        assert client.get(path).json()["mode"] == "server"
        assert registry.get("demo").subscription_url == source.subscription_url
        assert registry.get("demo").at_internal_url == "http://at:4567"
        assert client.put(path, json={"mode": "direct", "at_internal_url": ""}).status_code == 200
        assert registry.get("demo").at_internal_url is None


@pytest.mark.parametrize("body", [
    {"mode": "parallel", "at_internal_url": ""},
    {"mode": "server", "at_internal_url": "file:///tmp/a"},
    {"mode": "server", "at_internal_url": "http://user:secret@at:4567"},
    {"mode": "server", "at_internal_url": "http://at:4567/path?token=secret"},
    {"mode": "server", "at_internal_url": "http://at:4567/#secret"},
])
def test_invalid_cloud_settings_do_not_mutate_source(tmp_path, body):
    source = SourceConfig("demo", "Demo")
    registry = SourceRegistry((source,))
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), registry)) as client:
        response = client.put("/t4/api/sources/demo/playback", json=body)
        assert response.status_code in {400, 422}
        assert "secret" not in response.text
        assert registry.get("demo") == source
        assert client.put("/t4/api/sources/missing/playback", json={
            "mode": "direct", "at_internal_url": "",
        }).status_code == 404


@pytest.mark.parametrize("mode", ["direct", "server"])
@pytest.mark.parametrize("kind", ["builtin", "raw_python", "official_t4"])
def test_cloud_policy_uses_existing_backend_and_never_reparses_url(mode, kind):
    target = plugin(plugin_type=kind, **(
        {"api": "plugin-1", "t4_endpoint": "https://at.example/t4/v/private/plugin-1"}
        if kind == "official_t4" else {}
    ))
    original_config = json.dumps(target.local_proxy_config, sort_keys=True)
    source = SourceConfig("demo", "Demo", cloud_playback=mode)
    calls = []

    class Worker:
        def call(self, key, selected, method, params):
            calls.append(("worker", selected, method, params))
            return {"url": "https://at.example/at/p/fixture-token/1@99.mkv", "subs": []}

    def upstream(request):
        calls.append(("native", request))
        if kind == "builtin":
            assert request.url.params["id"] == "1@99"
            assert request.url.params.get("type", "") == ("" if mode == "server" else "client-proxy")
        else:
            assert request.url.params["play"] == "1@99"
        return httpx.Response(200, json={
            "url": "https://at.example/at/p/fixture-token/1@99.mkv",
            "header": {"User-Agent": "fixture-ua"}, "subs": [{"url": "https://at.example/sub.vtt"}],
        })

    native = NativeAtClient(transport=httpx.MockTransport(upstream))
    bridge = PlaybackBridge()
    adapter = T4Adapter(
        SourceRegistry((source,)), Worker(), native_invoker=NativeAtInvoker(native),
        local_snapshot_provider=lambda _: SubscriptionSnapshot("demo", 1, (target,)),
        playback_bridge=bridge,
    )
    try:
        result = asyncio.run(adapter.dispatch(T4Request(
            "play", "demo", "cloud", {"play": "1@99"}, "https://public.example",
        )))
        assert len(calls) == 1
        assert ("/t4/media/" in result["url"]) == (mode == "server")
        if mode == "server":
            asset = bridge.get(urlsplit(result["url"]).path.rsplit("/", 1)[-1])
            assert asset.kind == "at_proxy"
            assert asset.url == "https://at.example/at/p/fixture-token/1@99.mkv"
            assert result["header"] == {}
        if kind == "raw_python":
            assert calls[0][1].local_proxy_config == target.local_proxy_config
            assert bool(calls[0][3].get("_server_proxy")) == (mode == "server")
        else:
            assert result["subs"] == [{"url": "https://at.example/sub.vtt"}]
        assert json.dumps(target.local_proxy_config, sort_keys=True) == original_config
    finally:
        native.close()
        asyncio.run(bridge.aclose())
        bridge.close()


@pytest.mark.parametrize("url", [
    "https://cdn.example/file.mkv?sign=private",
    "https://other-at.example/at/p/fixture-token/1@99",
    "https://at.example/at/p/wrong-token/1@99",
    "https://at.example/at/p/fixture-token/1@99/extra",
    "https://at.example/at/play/fixture-token?id=1@99",
    "https://at.example.evil/at/p/fixture-token/1@99",
])
def test_relay_does_not_guess_routes_or_rewrite_external_links(url):
    bridge = PlaybackBridge()
    original = {"url": url, "header": {"Referer": "https://115.com/"}, "type": "PAN115"}
    try:
        assert bridge.relay_at(
            original, "https://public.example", backend_api="https://at.example/at",
            token="fixture-token", internal_url="http://at:4567",
        ) == original
        assert not bridge._assets
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()


def test_relay_maps_only_at_prefix_preserving_signed_query_and_headers():
    bridge = PlaybackBridge()
    original = {
        "url": "https://at.example/at/p/fixture-token/1@99.mkv?sign=exact%2Fvalue",
        "header": {"User-Agent": "signed-ua", "Referer": "https://115.com/"},
        "type": "PAN115", "subs": [{"url": "https://at.example/sub.vtt"}],
    }
    try:
        result = bridge.relay_at(
            original, "https://public.example", backend_api="https://at.example/at",
            token="fixture-token", internal_url="http://at:4567/internal",
        )
        asset = bridge.get(result["url"].rsplit("/", 1)[-1])
        assert asset.url == "http://at:4567/internal/p/fixture-token/1@99.mkv?sign=exact%2Fvalue"
        assert asset.headers == original["header"]
        assert result["header"] == {} and result["type"] == "PAN115"
        assert result["subs"] == original["subs"]
        assert original["url"].startswith("https://at.example/")
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()


def test_mode_switch_only_reinitializes_worker_when_effective_extend_changes():
    registry = SourceRegistry((SourceConfig("demo", "Demo"),))
    target = plugin(plugin_type="raw_python")
    configs = []

    class Worker:
        def call(self, key, selected, method, params):
            configs.append(json.loads(base64.b64decode(selected.worker_extend(
                server_proxy=bool(params.get("_server_proxy")),
            ))))
            return {"list": []}

    adapter = T4Adapter(registry, Worker(), local_snapshot_provider=lambda _:
                        SubscriptionSnapshot("demo", 1, (target,)))
    asyncio.run(adapter.dispatch(T4Request("home", "demo", "cloud", {})))
    registry.upsert(replace(registry.get("demo"), cloud_playback="server"))
    asyncio.run(adapter.dispatch(T4Request("home", "demo", "cloud", {})))
    registry.upsert(replace(registry.get("demo"), cloud_playback="direct"))
    asyncio.run(adapter.dispatch(T4Request("home", "demo", "cloud", {})))
    assert configs[0]["local_proxy_config"] == configs[2]["local_proxy_config"] != {}
    assert configs[1]["local_proxy_config"] == configs[0]["local_proxy_config"]
    assert configs[1]["_t4_server_proxy"] is True
    assert "_t4_server_proxy" not in configs[0] and "_t4_server_proxy" not in configs[2]


def test_failed_settings_write_keeps_memory_and_previous_file(tmp_path, monkeypatch):
    from pathlib import Path
    from sidecar.gateway.playback_settings import PlaybackSettingsStore

    source = SourceConfig("demo", "Demo")
    registry = SourceRegistry((source,))
    target = tmp_path / "playback.json"
    store = PlaybackSettingsStore(registry, target)
    store.update("demo", "direct", "")
    before = target.read_bytes()

    def denied(*_):
        raise PermissionError("fixture")

    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(PermissionError):
        store.update("demo", "server", "http://at:4567")
    assert registry.get("demo") == source
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


def test_multiple_sources_keep_settings_and_media_headers_isolated(tmp_path):
    registry = SourceRegistry((SourceConfig("a", "A"), SourceConfig("b", "B")))
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), registry)) as client:
        assert client.put("/t4/api/sources/a/playback", json={
            "mode": "server", "at_internal_url": "http://at-a:4567",
        }).status_code == 200
        assert client.get("/t4/api/sources/b/playback").json() == {
            "mode": "direct", "at_internal_url": "",
        }
    bridge = PlaybackBridge()
    try:
        results = [bridge.relay_at(
            {"url": f"https://at.example/p/{token}/1@99",
             "header": {"User-Agent": token}, "position": index},
            "https://public.example", backend_api="https://at.example",
            token=token, internal_url=f"http://at-{index}:4567",
        ) for index, token in enumerate(("token-a", "token-b"))]
        assets = [bridge.get(item["url"].rsplit("/", 1)[-1]) for item in results]
        assert len({item["url"] for item in results}) == 2
        assert [asset.headers["User-Agent"] for asset in assets] == ["token-a", "token-b"]
        assert [item["position"] for item in results] == [0, 1]
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()


def test_official_t4_token_is_derived_from_verified_endpoint_not_remote_ext():
    snapshot = SubscriptionParser("https://at.example").parse("demo", {"sites": [{
        "key": "plugin-1", "name": "Cloud", "type": 4,
        "api": "https://at.example/t4/fixture-token/v/plugin-1",
        "ext": {"token": "ignored-remote-ext"},
    }]})
    assert not snapshot.errors
    assert snapshot.plugins[0].token == "fixture-token"
    assert "fixture-token" not in json.dumps(snapshot.public_metadata())


@pytest.mark.parametrize("during_run", [False, True])
def test_gate_is_invalidated_when_cloud_route_changes(tmp_path, during_run):
    from fastapi import FastAPI
    from sidecar.gateway.routes.compatibility import build_compatibility_router
    from tests.unit.test_compatibility import runner_for, run_once

    runner, bridge = runner_for(tmp_path)

    def changed():
        source = runner.sources.get("test")
        runner.sources.upsert(replace(
            source, cloud_playback="server", at_internal_url="http://at-internal:4567",
        ))

    if during_run:
        def hook(request):
            if request.module == "play":
                changed()
        runner.adapter.hook = hook
    try:
        result = run_once(runner)
        assert result["gate"]["passed"] is (not during_run)
        if not during_run:
            changed()
        app = FastAPI()
        app.include_router(build_compatibility_router(runner))
        with TestClient(app) as client:
            report = client.get("/t4/api/compatibility/latest").json()["report"]
            assert report["playback_current"] is False
            assert report["gate"]["passed"] is False
            assert report["gate"]["reason"] == "playback_settings_changed"
            assert "at-internal" not in json.dumps(report)
    finally:
        runner.probes.close()
        asyncio.run(bridge.aclose())
        bridge.close()


@pytest.mark.parametrize("api", ["csp_Emby", "csp_Jellyfin", "csp_FeiNiu"])
def test_cloud_setting_does_not_change_non_cloud_play_parameters(api):
    query = NativeAtClient._play_query(plugin(api=api), {
        "id": "original-id", "_server_proxy": True,
    })
    assert query["type"] == "client-proxy"
    assert query["id"] == "original-id"
