import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.at.native import (
    NativeAtClient, NativeAtInvoker, NativeAtError, NativeAtUnsupportedError,
)
from sidecar.at.subscription import SubscriptionParser, resolve_subscription
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.adapter import T4Adapter


AT = "https://at.example"
TOKEN = "fixture-private-token"
BUILTINS = (
    "AList", "XiaoYa", "PianDan", "Media", "BiliBili", "Emby", "Jellyfin",
    "FeiNiu", "Live", "TgDouBan", "TgChannel", "TgWeb", "FishPanSou", "FishPanSouGroup",
)


def official_site(site="Media", *, base=AT, token=TOKEN):
    return {
        "key": "csp_" + site, "name": site, "type": 4,
        "api": f"{base}/t4/{token}/v/{site}",
        "searchable": 1, "filterable": 1,
    }


class NoWorker:
    def call(self, *args):
        raise AssertionError("an official builtin must not start a plugin Worker")


@pytest.mark.parametrize("name", BUILTINS)
@pytest.mark.parametrize("prefixed", [False, True])
def test_official_descriptors_preserve_identity_and_hide_endpoint(name, prefixed):
    site = official_site(name)
    if prefixed:
        site["api"] = site["api"].replace("/v/", "/v/csp_")
    snapshot = SubscriptionParser(AT).parse("at", {"sites": [site]})
    assert snapshot.errors == ()
    plugin = snapshot.plugins[0]
    assert plugin.plugin_id == plugin.api == "csp_" + name
    assert plugin.plugin_type == "builtin"
    assert plugin.t4_endpoint == site["api"]
    assert plugin.site_options["filterable"] == 1
    metadata = snapshot.public_metadata()
    assert metadata["plugins"][0]["transport"] == "official_t4"
    assert TOKEN not in json.dumps(metadata)
    assert TOKEN not in repr(plugin)


@pytest.mark.parametrize("api", [
    "https://elsewhere.example/t4/fixture-private-token/v/Media",
    f"{AT}/play/{TOKEN}",
    f"{AT}/t4/{TOKEN}/v/Unknown",
    f"{AT}/t4/{TOKEN}/v/Media?token=hidden",
    f"{AT}/t4/{TOKEN}/v/Media#hidden",
    f"{AT}/t4/{TOKEN}/v/Media/extra",
    f"https://name:{TOKEN}@at.example/t4/-/v/Media",
])
def test_invalid_type4_endpoints_are_accounted_for_without_secrets(api):
    site = {**official_site(), "api": api}
    snapshot = SubscriptionParser(AT).parse("at", {"sites": [site]})
    assert snapshot.plugins == ()
    assert len(snapshot.errors) == len(snapshot.unresolved_sites) == 1
    assert TOKEN not in json.dumps(snapshot.public_metadata())
    assert api not in json.dumps(snapshot.public_metadata())


def test_official_t4_coexists_with_raw_encrypted_and_client_entries():
    raw = {
        "key": "raw", "api": "csp_PyProxy", "type": 3,
        "ext": {"api": AT, "source": AT + "/plugins/sub/1.py",
                "loader": AT + "/Atvp/token/v1.py", "raw": True},
    }
    encrypted = {
        "key": "encrypted", "api": "csp_PyProxy", "type": 3,
        "ext": {"api": AT, "source": AT + "/plugins/sub/2.txt",
                "loader": AT + "/Atvp/token/v1.py", "secret": "fixture-secret"},
    }
    snapshot = SubscriptionParser(AT).parse("at", {
        "spider": AT + "/spring.jar",
        "sites": [
            official_site(), raw, encrypted,
            {"key": "push_agent", "api": "csp_Push", "type": 3},
            {"key": "home", "api": "csp_WebHome", "type": 3,
             "homePage": "https://home.example"},
        ],
    })
    assert snapshot.errors == ()
    assert [plugin.plugin_type for plugin in snapshot.plugins] == [
        "builtin", "raw_python", "secspider", "builtin", "builtin",
    ]
    assert snapshot.plugins[1].worker_extend()
    assert snapshot.plugins[2].secret == "fixture-secret"
    assert snapshot.plugins[3].native_client_site["jar"] == AT + "/spring.jar"
    assert snapshot.plugins[4].public_metadata()["compatibility_mode"] == "page_entry_only"


def test_complete_official_subscription_uses_existing_origin_inference():
    source = SourceConfig("at", "AT", subscription_url=f"{AT}/t4/{TOKEN}/1")

    async def fetch(_source):
        return {"sites": [official_site()]}

    snapshot = asyncio.run(resolve_subscription(source, fetch))
    assert snapshot.errors == ()
    assert snapshot.plugins[0].t4_endpoint == official_site()["api"]


def test_configured_at_path_prefix_and_tokenless_endpoint():
    base = AT + "/at"
    snapshot = SubscriptionParser(base).parse("at", {
        "sites": [official_site(base=base, token="-")],
    })
    assert snapshot.errors == ()
    assert snapshot.plugins[0].t4_endpoint == f"{base}/t4/-/v/Media"


@pytest.mark.parametrize("name", BUILTINS)
def test_all_official_builtins_use_the_declared_endpoint_for_home(name):
    seen = []
    native = NativeAtClient(transport=httpx.MockTransport(
        lambda request: (seen.append(request) or httpx.Response(200, json={"list": []}))
    ))
    plugin = SubscriptionParser(AT).parse("at", {"sites": [official_site(name)]}).plugins[0]
    try:
        native.call(plugin, "home", {"filter": True})
    finally:
        native.close()
    assert len(seen) == 1  # The official endpoint already merges recommendations.
    assert str(seen[0].url).split("?")[0] == plugin.t4_endpoint
    assert seen[0].method == "GET"


@pytest.mark.parametrize("name", BUILTINS)
@pytest.mark.parametrize("method,params,expected", [
    ("category", {"tid": "all", "pg": "2"}, {"t": "all", "pg": "2", "ac": "detail"}),
    ("search", {"key": "Fixture", "quick": False, "pg": "3"},
     {"wd": "Fixture", "quick": "false", "pg": "3"}),
    ("detail", {"ids": ["fixture-item"]}, {"ids": "fixture-item", "ac": "detail"}),
    ("play", {"id": "fixture-episode", "flag": "HD"},
     {"play": "fixture-episode", "flag": "HD"}),
])
def test_official_wire_contract_for_every_builtin(name, method, params, expected):
    seen = []
    native = NativeAtClient(transport=httpx.MockTransport(
        lambda request: (seen.append(request) or httpx.Response(200, json={"list": []}))
    ))
    plugin = SubscriptionParser(AT).parse("at", {"sites": [official_site(name)]}).plugins[0]
    try:
        if name == "PianDan" and method in {"detail", "play"}:
            with pytest.raises(NativeAtUnsupportedError):
                native.call(plugin, method, params)
            assert seen == []  # The adapter, not AT's empty detail, owns navigation.
            return
        native.call(plugin, method, params)
    finally:
        native.close()
    assert len(seen) == 1
    query = dict(seen[0].url.params)
    if method == "category":
        encoded = query.pop("ext")
        assert json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))) == {}
    assert query == expected
    assert seen[0].url.path == f"/t4/{TOKEN}/v/{name}"


def make_app(tmp_path, sites, backend):
    async def fetcher(_source):
        return {"sites": sites}

    registry = SourceRegistry((
        SourceConfig("at", "AT", subscription_url=f"{AT}/t4/{TOKEN}/1"),
    ))
    native = NativeAtClient(transport=httpx.MockTransport(backend))
    adapter = T4Adapter(
        registry, NoWorker(), subscription_fetcher=fetcher,
        native_invoker=NativeAtInvoker(native),
    )
    return native, create_app(
        Settings(plugin_dir=str(tmp_path)), registry,
        subscription_fetcher=fetcher, t4_adapter=adapter,
    )


@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
@pytest.mark.parametrize("entry", ["fm", "fm_module", "module"])
def test_client_contract_category_detail_play_and_action(tmp_path, transport, entry):
    seen = []
    raw_id = "1@fixture-item"
    raw_play = "1@fixture-file"
    media = "https://media.example/movie.mkv?signature=fixture"
    cover = "https://image.example/poster.jpg"
    filters = {"genre": "科幻", "year": "2026", "sort": "date"}
    headers = {"Referer": "https://drive.example", "User-Agent": "fixture"}

    def backend(request):
        seen.append(request)
        assert request.method == "GET"  # Official AT currently exposes only GET.
        assert request.url.path == f"/t4/{TOKEN}/v/Media"
        query = request.url.params
        if "play" in query:
            if query["play"] == "msubinspect-20":
                return httpx.Response(200, json={"msg": "已检查"})
            assert query["play"] == raw_play
            return httpx.Response(200, json={
                "url": media, "parse": 0, "header": json.dumps(headers),
                "type": "PAN115", "subs": [{"name": "中文", "url": "https://sub.example/1.srt"}],
                "danmaku": "https://comment.example/1.xml", "chapters": [{"name": "片头", "start": 0}],
            })
        item = {"vod_id": raw_id, "vod_name": "Fixture", "vod_pic": cover}
        if "ids" in query:
            assert dict(query) == {"ac": "detail", "ids": raw_id}
            item.update(vod_play_from="正片", vod_play_url="第一集$" + raw_play)
        if "t" in query:
            assert query["t"] == raw_id
            assert query["pg"] == "2"
            encoded = query["ext"]
            assert json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))) == filters
        if "wd" in query:
            assert dict(query) == {"wd": "Fixture", "quick": "false", "pg": "3"}
        return httpx.Response(200, json={
            "class": [{"type_id": "all", "type_name": "全部"}],
            "filters": {"all": [{"key": "genre", "value": []}]},
            "list": [item], "page": int(query.get("pg", "1")),
        })

    native, app = make_app(tmp_path, [official_site()], backend)
    try:
        with TestClient(app) as client:
            config = client.get("/t4/config.json")
            site = config.json()["sites"][0]
            assert site["type"] == 4
            assert TOKEN not in config.text
            assert site["api"].endswith("/t4/fm/at/csp_Media/")
            metadata = client.get("/t4/api/sources/at/subscription")
            assert TOKEN not in metadata.text
            assert metadata.json()["plugins"][0]["transport"] == "official_t4"

            def call(module, params):
                path = f"/t4/fm/at/csp_Media/"
                if entry == "fm_module":
                    path += "api/" + module
                elif entry == "module":
                    path = "/t4/api/" + module
                    params = {**params, "source": "at", "site": "csp_Media"}
                if transport == "GET":
                    return client.get(path, params=params)
                if transport == "JSON":
                    return client.post(path, json=params)
                return client.post(path, data=params)

            home = call("home", {"filter": "true"}).json()
            assert home["class"][0]["type_id"] == "all"
            assert home["filters"]["all"][0]["key"] == "genre"
            card_id = home["list"][0]["vod_id"]
            assert card_id.startswith("t4_") and card_id != raw_id
            encoded = base64.urlsafe_b64encode(json.dumps(filters).encode()).decode().rstrip("=")
            category_params = {"t": card_id, "pg": 2, "ac": "detail"}
            category_params["extend" if entry != "fm" else "ext"] = (
                json.dumps(filters) if entry != "fm" else encoded
            )
            category = call("category", category_params)
            assert category.status_code == 200
            assert category.json()["page"] == 2
            assert call("search", {"wd": "Fixture", "pg": 3, "quick": False}).status_code == 200
            detail = call("detail", {"ac": "detail", "ids": card_id})
            assert detail.status_code == 200
            vod = detail.json()["list"][0]
            assert vod["vod_pic"] == cover
            play_id = vod["vod_play_url"].partition("$")[2]
            assert play_id != raw_play
            played = call("play", {"play": play_id, "flag": "正片"})
            assert played.status_code == 200
            payload = played.json()
            assert payload["url"] == media
            assert payload["header"] == headers
            assert payload["type"] == "PAN115"
            assert payload["subs"][0]["name"] == "中文"
            assert payload["danmaku"] and payload["chapters"]
            action = call("action", {"ac": "action", "id": "msubinspect-20"})
            assert action.status_code == 200
            assert action.json()["msg"] == "已检查"
            assert all("ac" not in request.url.params for request in seen if "play" in request.url.params)
            events = client.get("/t4/api/diagnostics/events").json()["events"]
            assert TOKEN not in json.dumps(events)
            assert any(event.get("backend") == "official_t4" for event in events)
    finally:
        native.close()


@pytest.mark.parametrize("failure", ["status", "json", "shape", "transport"])
def test_official_errors_are_redacted_without_trying_other_routes(failure):
    seen = []

    def backend(request):
        seen.append(request)
        if failure == "transport":
            raise httpx.ConnectError(TOKEN, request=request)
        if failure == "status":
            return httpx.Response(500, text=TOKEN)
        if failure == "shape":
            return httpx.Response(200, json=[TOKEN])
        return httpx.Response(200, text=TOKEN)

    native = NativeAtClient(transport=httpx.MockTransport(backend))
    plugin = SubscriptionParser(AT).parse("at", {"sites": [official_site()]}).plugins[0]
    try:
        with pytest.raises(NativeAtError) as caught:
            native.call(plugin, "home", {})
        assert TOKEN not in str(caught.value)
        assert len(seen) == 1
    finally:
        native.close()


def test_official_refresh_rotates_endpoint_without_changing_public_site_key(tmp_path):
    revision = "first"

    async def fetcher(_source):
        return {"sites": [official_site(token=revision)]}

    source = SourceConfig("at", "AT", subscription_url=AT + "/t4/-/1")
    with TestClient(create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source,)), fetcher,
    )) as client:
        original = client.get("/t4/config.json").json()["sites"][0]
        revision = "second"
        client.get("/t4/api/sources/at/subscription?refresh=true")
        current = client.get("/t4/config.json").json()["sites"][0]
        assert original == current


@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_official_piandan_keeps_title_navigation_and_delegates_to_official_media(tmp_path, transport):
    seen = []
    card = {"vod_id": "tmdb:movie:1", "vod_name": "Fixture Film", "vod_year": "2026"}

    def backend(request):
        seen.append(request)
        query = request.url.params
        if request.url.path.endswith("/PianDan"):
            assert not any(key in query for key in ("ids", "play"))
            return httpx.Response(200, json={"list": [card]})
        assert request.url.path.endswith("/Media")
        if "play" in query:
            assert query["play"] == "fixture-episode"
            return httpx.Response(200, json={"url": "https://media.example/episode.mp4"})
        if "wd" in query:
            assert query["wd"] == card["vod_name"]
        item = {**card, "vod_id": "msub:20"}
        if "ids" in query:
            assert query["ids"] == "msub:20"
            item.update(vod_play_from="正片", vod_play_url="第一集$fixture-episode")
        return httpx.Response(200, json={"list": [item]})

    native, app = make_app(tmp_path, [official_site("PianDan"), official_site()], backend)
    try:
        with TestClient(app) as client:
            config = client.get("/t4/config.json").json()["sites"][0]
            assert config["indexs"] == 1
            assert config["searchable"] == config["quickSearch"] == 0

            def call(params):
                if transport == "GET":
                    return client.get(config["api"], params=params)
                if transport == "JSON":
                    return client.post(config["api"], json=params)
                return client.post(config["api"], data=params)

            home = call({}).json()
            detail = call({"ac": "detail", "ids": home["list"][0]["vod_id"]})
            assert detail.status_code == 200
            vod = detail.json()["list"][0]
            value = vod["vod_play_url"].partition("$")[2]
            played = call({"play": value, "flag": vod["vod_play_from"]})
            assert played.status_code == 200
            assert played.json()["url"] == "https://media.example/episode.mp4"
            assert [request.url.path.rsplit("/", 1)[-1] for request in seen] == [
                "PianDan", "Media", "Media", "Media",
            ]
    finally:
        native.close()


def test_bilibili_official_short_url_and_live_message_are_not_reinterpreted(tmp_path):
    short_url = AT + f"/proxy/{TOKEN}/fixture-bvid"

    def backend(request):
        if request.url.path.endswith("/BiliBili"):
            return httpx.Response(200, json={"url": short_url, "parse": 0, "jx": 0})
        assert request.url.path.endswith("/Live")
        assert request.url.params["play"] == "offline"
        return httpx.Response(200, json={"msg": "直播间未开播"})

    native, app = make_app(tmp_path, [official_site("BiliBili"), official_site("Live")], backend)
    try:
        with TestClient(app) as client:
            play = client.get("/t4/fm/at/csp_BiliBili/", params={"play": "fixture-bvid"})
            assert play.status_code == 200
            # Do not fetch the short URL eagerly, invent metadata or advertise DASH.
            assert play.json()["url"] == short_url
            assert "subs" not in play.json()
            assert "format" not in play.json()
            offline = client.get("/t4/fm/at/csp_Live/", params={"play": "offline"})
            assert offline.status_code == 200
            assert offline.json()["msg"] == "直播间未开播"
            unsupported = client.post("/t4/fm/at/csp_Live/", json={
                "ac": "action", "action": "unknown",
            })
            assert unsupported.status_code == 501
    finally:
        native.close()


def test_app_interactive_and_gate_adapters_share_official_client_and_close_it(tmp_path, monkeypatch):
    from sidecar.gateway import main
    from tests.unit.test_compatibility import PNG, MP4

    seen = []

    def backend(request):
        seen.append(request)
        query = request.url.params
        if "play" in query:
            assert query["play"] == "fixture-episode"  # Never a destructive action track.
            return httpx.Response(200, json={"url": "https://media.example/video.mp4"})
        item = {"vod_id": "msub:20", "vod_name": "Fixture Film",
                "vod_pic": "https://media.example/cover"}
        if "ids" in query:
            item.update(vod_play_from="正片$$$操作",
                        vod_play_url="第一集$fixture-episode$$$删除$msubdelete-20")
        return httpx.Response(200, json={
            "class": [{"type_id": "all", "type_name": "全部"}], "list": [item],
        })

    native = NativeAtClient(transport=httpx.MockTransport(backend))
    monkeypatch.setattr(main, "NativeAtClient", lambda **_kwargs: native)
    monkeypatch.setattr(main.AtvpWorkerInvoker, "call", NoWorker().call)

    async def fetcher(_source):
        return {"sites": [official_site()]}

    def resources(request):
        if request.url.path == "/cover":
            return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})
        start, end = map(int, request.headers["range"][6:].split("-"))
        end = min(end, len(MP4) - 1)
        return httpx.Response(206, content=MP4[start:end + 1], headers={
            "Content-Range": f"bytes {start}-{end}/{len(MP4)}", "Content-Type": "video/mp4",
        })

    registry = SourceRegistry((SourceConfig("at", "AT", subscription_url=f"{AT}/t4/-/1"),))
    app = create_app(
        Settings(plugin_dir=str(tmp_path), compatibility_report=str(tmp_path / "gate.json")),
        registry, fetcher,
    )
    gate = app.state.compatibility
    gate.probes.client.close()
    gate.probes.client = httpx.Client(transport=httpx.MockTransport(resources))
    with TestClient(app) as client:
        assert client.get("/t4/fm/at/csp_Media/").status_code == 200
        response = client.post("/t4/api/compatibility/runs", json={})
        assert response.status_code == 202

        async def finish():
            await gate.task
            return gate.report

        report = client.portal.call(finish)
        assert report["state"] == "completed"
        assert report["gate"]["passed"] is True  # Only the existing server-sample gate.
        assert report["real_device_verified"] is False
        assert report["long_play_verified"] is False
        assert report["plugins"][0]["status"] == "pass"
        assert all(request.url.path == f"/t4/{TOKEN}/v/Media" for request in seen)
        assert TOKEN not in json.dumps(report)
        assert TOKEN not in (tmp_path / "gate.json").read_text(encoding="utf-8")
    assert native._client.is_closed


def test_official_and_atvp_plugins_keep_distinct_execution_owners(tmp_path):
    from tests.t4.test_adapter import FakeWorker

    calls = []

    class Worker(FakeWorker):
        def call(self, runtime_key, plugin, method, params):
            calls.append((plugin.plugin_id, plugin.plugin_type, method))
            return super().call(runtime_key, plugin, method, params)

    sites = [
        official_site(),
        {"key": "raw", "api": "csp_PyProxy", "type": 3,
         "ext": {"source": AT + "/plugin.py", "raw": True}},
        {"key": "encrypted", "api": "csp_PyProxy", "type": 3,
         "ext": {"source": AT + "/plugin.txt", "loader": AT + "/Atvp.py"}},
    ]

    async def fetcher(_):
        return {"sites": sites}

    registry = SourceRegistry((SourceConfig("at", "AT", subscription_url=AT + "/t4/-/1"),))
    native = NativeAtClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"list": []})
    ))
    adapter = T4Adapter(
        registry, Worker(), subscription_fetcher=fetcher,
        native_invoker=NativeAtInvoker(native),
    )
    try:
        with TestClient(create_app(
            Settings(plugin_dir=str(tmp_path)), registry,
            subscription_fetcher=fetcher, t4_adapter=adapter,
        )) as client:
            config = client.get("/t4/config.json").json()
            assert len(config["sites"]) == 3
            for site in config["sites"]:
                assert client.get(site["api"]).status_code == 200
            assert calls == [("raw", "raw_python", "home"), ("encrypted", "secspider", "home")]
    finally:
        native.close()
