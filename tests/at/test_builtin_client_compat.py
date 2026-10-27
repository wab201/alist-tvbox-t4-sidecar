import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.at.native import NativeAtClient, NativeAtInvoker
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.adapter import T4Adapter
from tests.t4.test_adapter import FakeWorker


AT = "https://at.example"
PUSH_EXT = base64.b64encode(json.dumps({
    "api": AT, "token": "original-token", "secret": "original-secret",
    "local_proxy_config": {"test": {"enabled": True}},
}).encode()).decode()
PUSH = {
    "key": "push_agent", "name": "AT Push", "api": "csp_Push", "type": 3,
    "ext": PUSH_EXT, "jar": f"{AT}/spring.jar;md5;original",
    "searchable": 0, "quickSearch": 0, "filterable": 1, "changeable": 0,
}


def source(source_id="at"):
    return SourceConfig(source_id, "AT", subscription_url=f"{AT}/sub/fixture/1")


async def fetcher(source_config):
    push = dict(PUSH)
    if source_config.source_id == "second":
        push.update(
            name="Second Push",
            jar=f"{AT}/other.jar",
            ext={"api": AT, "token": "second-token"},
        )
    return {"sites": [
        {
            "key": "csp_PianDan", "name": "PianDan", "api": "csp_PianDan",
            "ext": {"api": AT, "token": "original-token"},
            "indexs": 1, "searchable": 0, "quickSearch": 0, "filterable": 1,
        },
        push,
    ]}


@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_piandan_unified_subscription_home_filters_and_title_navigation(tmp_path, transport):
    seen = []
    classes = [{"type_id": "tmdb:trending", "type_name": "TMDB"}]
    filters = {"tmdb:trending": [{"key": "mediaType", "name": "Media", "value": []}]}
    card = {
        "vod_id": "tmdb:movie:1", "vod_name": "Example Film",
        "vod_pic": "https://image.example/poster.jpg", "vod_year": "2026",
    }

    def handler(request):
        seen.append(request)
        assert request.url.path == "/pian-dan/original-token"
        assert request.headers["X-CLIENT"] == "test-client"
        if not request.url.params:
            return httpx.Response(200, json={"class": classes, "filters": filters})
        return httpx.Response(200, json={
            "list": [card], "page": int(request.url.params.get("pg", "1")),
        })

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
            sites = client.get("/t4/config.json").json()["sites"]
            navigation = sites[0]
            assert navigation["type"] == 4
            assert navigation["indexs"] == 1
            assert navigation["searchable"] == navigation["quickSearch"] == 0
            assert sites[1] == PUSH
            api = navigation["api"]

            def call(params):
                kwargs = {"headers": {"X-CLIENT": "test-client"}}
                if transport == "GET":
                    return client.get(api, params=params, **kwargs)
                if transport == "JSON":
                    return client.post(api, json=params, **kwargs)
                return client.post(api, data=params, **kwargs)

            home = call({"filter": "true"})
            assert home.status_code == 200
            assert home.json()["class"] == classes
            assert home.json()["filters"] == filters
            assert home.json()["list"][0]["vod_name"] == card["vod_name"]
            assert home.json()["list"][0]["vod_pic"] == card["vod_pic"]
            assert len(seen) == 2
            assert dict(seen[-1].url.params) == {"t": "0"}
            values = {
                "mediaType": "tv", "time_window": "day", "with_genres": "16",
                "sort_by": "vote_average.desc", "vote_average.gte": "8",
                "year": "2026", "t": "wrong-category", "pg": "999",
            }
            ext = base64.urlsafe_b64encode(json.dumps(values).encode()).decode().rstrip("=")
            category = call({"ac": "detail", "t": "tmdb:trending", "pg": "2", "ext": ext})
            assert category.status_code == 200
            assert category.json()["page"] == 2
            query = dict(seen[-1].url.params)
            assert query["t"] == "tmdb:trending"
            assert query["pg"] == "2"
            for key in values.keys() - {"t", "pg"}:
                assert query[key] == values[key]
            assert not any(key.startswith("filter.") for key in query)
            search = call({"wd": "Example Film", "pg": "3", "quick": "false"})
            assert search.status_code == 200
            assert seen[-1].url.params["wd"] == "Example Film"
            assert seen[-1].url.params["pg"] == "3"
            assert seen[-1].url.params["quick"] == "false"
            # FongMi uses indexs. Other clients request detail and need title lookup.
            count = len(seen)
            missing = call({"ac": "detail", "ids": home.json()["list"][0]["vod_id"]})
            assert missing.status_code == 400  # This subscription has no searchable VOD source.
            assert "vod_play_url" not in missing.text
            for params in (
                {"play": "tmdb:movie:1"},
                {"ac": "action", "action": "play", "id": "tmdb:movie:1"},
            ):
                response = call(params)
                assert response.status_code == 501
                assert response.json()["detail"]["code"] == "t4_builtin_unsupported"
            assert len(seen) == count
    finally:
        native.close()


def test_native_push_export_is_singleton_and_does_not_leak_into_metadata(tmp_path):
    registry = SourceRegistry((source(), source("second")))
    app = create_app(
        Settings(plugin_dir=str(tmp_path)), registry, subscription_fetcher=fetcher,
    )
    with TestClient(app) as client:
        config = client.get("/t4/config.json")
        assert config.status_code == 200
        sites = config.json()["sites"]
        assert len(sites) == 3
        assert len({site["key"] for site in sites}) == 3
        assert [site for site in sites if site["key"] == "push_agent"] == [PUSH]
        assert len([site for site in sites if site["type"] == 4]) == 2
        snapshot = client.get("/t4/api/sources/at/subscription")
        plugin = snapshot.json()["plugins"][1]
        assert plugin["client_config_path"] == "/t4/config.json"
        assert plugin["client_supported"] is True
        assert "fm_path" not in plugin
        assert PUSH_EXT not in snapshot.text
        assert "original-token" not in snapshot.text
        assert "original-secret" not in snapshot.text
        # The original csp_Push remains client-owned, not a fake server catalog.
        response = client.get("/t4/fm/at/push_agent/")
        assert response.status_code == 501


@pytest.mark.parametrize("options", [
    {},
    {"indexs": 0, "searchable": 1, "quickSearch": 1, "filterable": 0},
])
def test_piandan_always_uses_original_navigation_capabilities(tmp_path, options):
    async def minimal(_source):
        return {"sites": [{"key": "csp_PianDan", "api": "csp_PianDan", **options}]}

    app = create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=minimal,
    )
    with TestClient(app) as client:
        site = client.get("/t4/config.json").json()["sites"][0]
        assert site["indexs"] == 1
        assert site["searchable"] == site["quickSearch"] == 0
        assert site["filterable"] == 1


@pytest.mark.parametrize("site_jar", [None, f"{AT}/site-specific.jar;md5;original"])
@pytest.mark.parametrize("ext", [PUSH_EXT, {"api": AT, "token": "original-token"}])
def test_push_client_config_preserves_site_fields_and_resolves_global_jar(
    tmp_path, site_jar, ext,
):
    original = {
        **PUSH,
        "key": "renamed-push",
        "ext": ext,
        "header": {"User-Agent": "original-client"},
        "timeout": 60,
        "style": {"type": "rect", "ratio": 1.5},
    }
    original.pop("jar")
    if site_jar is not None:
        original["jar"] = site_jar
    global_jar = f"{AT}/spring.jar;md5;global"

    async def original_subscription(_source):
        return {"spider": global_jar, "sites": [original]}

    app = create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=original_subscription,
    )
    with TestClient(app) as client:
        response = client.get("/t4/config.json")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["sites"] == [{
            **original, "key": "push_agent", "jar": site_jar or global_jar,
        }]
        assert original["key"] == "renamed-push"
        metadata = client.get("/t4/api/sources/at/subscription")
        plugin = metadata.json()["plugins"][0]
        assert plugin["client_supported"] is True
        assert plugin["client_config_path"] == "/t4/config.json"
        assert "original-token" not in metadata.text
        assert PUSH_EXT not in metadata.text


def test_push_refresh_keeps_client_key_but_updates_original_jar_and_ext(tmp_path):
    revision = 1

    async def changing(_source):
        return {"sites": [{
            **PUSH,
            "jar": f"{AT}/spring.jar;md5;revision-{revision}",
            "ext": {"api": AT, "token": f"token-{revision}"},
        }]}

    app = create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=changing,
    )
    with TestClient(app) as client:
        first = client.get("/t4/config.json").json()["sites"][0]
        revision = 2
        assert client.get("/t4/config.json").json()["sites"][0] == first
        assert client.get(
            "/t4/api/sources/at/subscription?refresh=true"
        ).status_code == 200
        current = client.get("/t4/config.json").json()["sites"][0]
        assert current["key"] == first["key"] == "push_agent"
        assert current["jar"].endswith("revision-2")
        assert current["ext"]["token"] == "token-2"


def test_push_without_original_jar_is_not_exported_as_fake_t4(tmp_path):
    async def incomplete(_source):
        return {"sites": [{"key": "push_agent", "api": "csp_Push", "type": 3}]}

    app = create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=incomplete,
    )
    with TestClient(app) as client:
        assert client.get("/t4/config.json").json() == {"sites": []}
        plugin = client.get(
            "/t4/api/sources/at/subscription"
        ).json()["plugins"][0]
        assert plugin["client_supported"] is False
        assert plugin["t4_supported"] is False
        assert "client_config_path" not in plugin


@pytest.mark.parametrize("entry", ["module", "fm"])
def test_piandan_web_smoke_and_fm_use_the_same_default_client(tmp_path, entry):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["X-CLIENT"] == "com.fongmi.android.tv"
        if not request.url.params:
            return httpx.Response(200, json={
                "class": [{"type_id": "tmdb:trending"}],
                "filters": {"tmdb:trending": []},
            })
        assert dict(request.url.params) == {"t": "0"}
        return httpx.Response(200, json={"list": [{
            "vod_id": "tmdb:movie:1", "vod_name": "Film",
            "vod_pic": "https://image.example/poster.jpg",
        }]})

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
            if entry == "module":
                response = client.get(
                    "/t4/api/home", params={"source": "at", "site": "csp_PianDan"}
                )
            else:
                api = client.get("/t4/config.json").json()["sites"][0]["api"]
                response = client.get(api, params={"filter": "true"})
            assert response.status_code == 200
            assert response.json()["class"] == [{"type_id": "tmdb:trending"}]
            assert response.json()["filters"] == {"tmdb:trending": []}
            assert response.json()["list"][0]["vod_name"] == "Film"
            assert len(seen) == 2
    finally:
        native.close()


@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_piandan_non_index_client_searches_then_delegates_real_playback(tmp_path, transport):
    card = {"vod_id": "tmdb:movie:1", "vod_name": "Example Film", "vod_year": "2026"}
    calls = []

    async def subscription(_source):
        payload = await fetcher(_source)
        payload["sites"].append({
            "key": "vod-demo", "name": "VOD", "api": "csp_PyProxy", "searchable": 1,
            "ext": {"source": "https://at.example/demo.py", "raw": True},
        })
        return payload

    def handler(request):
        assert request.url.path.startswith("/pian-dan/")
        return httpx.Response(200, json={"list": [card]})

    class MatchingWorker(FakeWorker):
        def call(self, runtime_key, plugin, method, params):
            calls.append((plugin.plugin_id, method, params))
            if method == "search":
                assert params["key"] == "Example Film"
                return {"list": [
                    {"vod_id": "wrong", "vod_name": "Example Film sequel"},
                    {"vod_id": "wrong-year", "vod_name": "Example Film", "vod_year": "2025"},
                    {"vod_id": "correct", "vod_name": "Example Film", "vod_year": "2026"},
                ]}
            if method == "detail":
                assert params["ids"] == ["correct"]
            return super().call(runtime_key, plugin, method, params)

    registry = SourceRegistry((source(),))
    native = NativeAtClient(transport=httpx.MockTransport(handler))
    adapter = T4Adapter(
        registry, MatchingWorker(), subscription_fetcher=subscription,
        native_invoker=NativeAtInvoker(native),
    )
    try:
        with TestClient(create_app(
            Settings(plugin_dir=str(tmp_path)), registry,
            subscription_fetcher=subscription, t4_adapter=adapter,
        )) as client:
            api = "/t4/fm/at/csp_PianDan/"

            def call(params):
                if transport == "GET":
                    return client.get(api, params=params)
                if transport == "JSON":
                    return client.post(api, json=params)
                return client.post(api, data=params)

            home = call({}).json()
            detail = call({"ids": home["list"][0]["vod_id"]})
            assert detail.status_code == 200
            vod = detail.json()["list"][0]
            assert vod["vod_name"] == "Example Film"
            assert vod["vod_play_from"].startswith("VOD")
            play_id = vod["vod_play_url"].partition("$")[2]
            play = call({"play": play_id, "flag": vod["vod_play_from"]})
            assert play.status_code == 200
            assert play.json()["url"] == "https://video.invalid/demo.m3u8"
            assert calls[-1][0] == "vod-demo"
            assert calls[-1][2]["id"] == "raw-play-id"
            stages = [event["stage"] for event in app_events(client)]
            assert "navigation.search" in stages
            assert "navigation.play_delegate" in stages
    finally:
        native.close()


def app_events(client):
    return client.get("/t4/api/diagnostics/events").json()["events"]
