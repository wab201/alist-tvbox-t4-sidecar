import io
import hashlib
import json
from pathlib import Path
import struct
from urllib.parse import parse_qs, urlsplit
import zipfile
import zlib

import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.client_acceleration import ClientAcceleration
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.profiles import T4ProfileStore
from sidecar.gateway.source_registry import SourceRegistry
from tests.unit.test_client_config import source, fetcher
from tests.unit.test_fm import FakeAdapter


class MediaAdapter(FakeAdapter):
    async def dispatch(self, request):
        self.requests.append(request)
        return {"url": "https://cdn.example/film.mp4", "parse": 0, "jx": 0,
                "header": {"User-Agent": "signed-agent", "Referer": "https://origin.example/"}}


@pytest.fixture
def client(tmp_path):
    app = create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=fetcher, t4_adapter=MediaAdapter(),
    )
    with TestClient(app, base_url="https://nas.example") as client:
        yield client


def play(client, api, value="demo"):
    # Match FM's Uri.Builder: retain the API query when adding playback parameters.
    return client.get(str(httpx.URL(api).copy_merge_params({"play": value})))


def offer(client, path="/t4/config.json", headers=None):
    response = client.get(path, headers=headers or {"User-Agent": "okhttp/4.12.0"})
    assert response.status_code == 200
    config = response.json()
    assert set(config) == {"sites"}
    assert all(site["type"] == 4 and "jar" not in site for site in config["sites"])
    component = client.get(response.links["urn:t4-sidecar:client-component"]["url"])
    assert component.status_code == 200
    descriptor = component.json()
    assert descriptor["optional"] is True
    assert descriptor["fallback"] == "t4"
    assert descriptor["sites"][-1]["hide"] == 1
    assert descriptor["sites"][-1]["searchable"] == 0
    jar = client.get(descriptor["spider"])
    assert jar.status_code == 200
    with zipfile.ZipFile(io.BytesIO(jar.content)) as archive:
        assert archive.read("classes.dex").startswith(b"dex\n")
        properties = dict(
            line.split("=", 1)
            for line in archive.read("t4-accelerator.properties").decode("ascii").splitlines()
        )
    return config, properties


def confirm(client, properties, **changes):
    return client.post(
        "/t4/acceleration/" + properties["session"] + "/ready",
        json={"proof": properties["challenge"], "protocol": 1, "version": "1", **changes},
    ).json()["ready"]


@pytest.mark.parametrize("headers", [
    {}, {"User-Agent": "okhttp/4.12"}, {"User-Agent": "Dalvik Android"},
    {"User-Agent": "FongMi/4"}, {"X-Client": "tvbox"},
    {"X-Client": "ios"}, {"X-Client": "web"}, {"X-Client": "fnmedia"},
    {"User-Agent": "Mozilla/5.0 (iPhone)"}, {"User-Agent": "Infuse"},
    {"User-Agent": "trim.media"}, {"User-Agent": "UnknownPlayer/1"},
])
def test_default_offer_is_brand_independent_and_does_not_require_loading(client, headers):
    response = client.get("/t4/config.json", headers=headers)
    assert response.status_code == 200
    config = response.json()
    assert set(config) == {"sites"}
    assert all(site["type"] == 4 and "jar" not in site for site in config["sites"])
    assert "urn:t4-sidecar:client-component" in response.links
    assert response.headers["cache-control"] == "no-store"
    assert "user-agent" not in response.headers.get("vary", "").lower()
    api = config["sites"][0]["api"]
    original = client.get(
        str(httpx.URL(api).copy_merge_params({"play": "demo"})), headers=headers,
    ).json()
    assert original == {
        "url": "https://cdn.example/film.mp4", "parse": 0, "jx": 0,
        "header": {"User-Agent": "signed-agent", "Referer": "https://origin.example/"},
    }
    assert client.get("/t4/api/client-acceleration").json()["ready"] == 0


def test_jar_download_does_not_enable_playback_and_bad_proofs_stay_plain(client):
    config, properties = offer(client)
    api = config["sites"][0]["api"]
    original = play(client, api).json()
    assert original["url"] == "https://cdn.example/film.mp4"
    assert not confirm(client, properties, proof="wrong")
    assert not confirm(client, properties, protocol=2)
    assert not confirm(client, properties, version="999")
    assert play(client, api).json() == original
    assert "t4_client" not in client.app.state.acceleration.summary()
    assert client.get("/t4/api/client-acceleration").json()["ready"] == 0


def test_pending_component_does_not_create_media_relays_or_plans(client, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("an unconfirmed component must not allocate a media relay")

    monkeypatch.setattr(client.app.state.playback, "client_relay", unexpected)
    config, properties = offer(client)
    assert play(client, config["sites"][0]["api"]).json() == (
        client.get("/t4/fm/env-at/bili/?play=demo").json()
    )
    assert not client.app.state.acceleration.get(properties["session"]).plans


@pytest.mark.parametrize("mode", ["pending", "ready"])
def test_expired_component_cannot_confirm_and_playback_returns_original(client, monkeypatch, mode):
    acceleration = client.app.state.acceleration
    now = [0.0]
    monkeypatch.setattr(acceleration, "clock", lambda: now[0])
    config, properties = offer(client)
    api = config["sites"][0]["api"]
    original = play(client, api).json()
    if mode == "ready":
        assert confirm(client, properties)
        assert play(client, api).json()["url"].startswith("proxy://")
    now[0] = (acceleration.ttl if mode == "ready" else acceleration.pending_ttl) + 1
    assert not confirm(client, properties)
    prefix = "/t4/acceleration/" + properties["session"]
    assert client.get(prefix + "/client.jar").status_code == 410
    assert client.get(prefix + "/component.json").status_code == 410
    assert play(client, api).json() == original
    assert acceleration.summary()["ready"] == 0


def test_missing_component_keeps_same_subscription_and_original_playback(client, tmp_path):
    config, proof = offer(client)
    original = play(client, config["sites"][0]["api"]).json()
    client.app.state.acceleration.jar = tmp_path / "missing.jar"
    response = client.get("/t4/config.json", headers={"X-Client": "unknown"})
    assert response.status_code == 200
    assert set(response.json()) == {"sites"}
    assert all(site["type"] == 4 for site in response.json()["sites"])
    assert "Link" not in response.headers
    assert "t4_client" not in response.text
    assert play(client, response.json()["sites"][0]["api"]).json() == original
    assert client.get(f"/t4/acceleration/{proof['session']}/client.jar").status_code == 410
    assert client.get("/t4/api/client-acceleration").json()["available"] is False
    assert play(client, config["sites"][0]["api"]).json() == original


@pytest.mark.parametrize("failure", [
    "download", "zip", "dex_missing", "dex_invalid", "decompression", "encrypted", "truncated",
])
def test_failed_component_never_breaks_import_or_playback(client, tmp_path, monkeypatch, failure):
    acceleration = client.app.state.acceleration
    if failure == "zip":
        acceleration.jar = tmp_path / "broken.jar"
        acceleration.jar.write_bytes(b"not a zip archive")
    elif failure in {"dex_missing", "dex_invalid"}:
        acceleration.jar = tmp_path / "broken.jar"
        with zipfile.ZipFile(acceleration.jar, "w") as archive:
            archive.writestr("other" if failure == "dex_missing" else "classes.dex", b"invalid")
    else:
        def unavailable(identity):
            error = {
                "download": OSError, "decompression": zlib.error,
                "encrypted": RuntimeError, "truncated": EOFError,
            }[failure]
            raise error("unavailable optional component")
        monkeypatch.setattr(acceleration, "artifact", unavailable)

    response = client.get("/t4/config.json")
    assert response.status_code == 200
    # A strict T4-only importer need not understand a JAR or any extra JSON field.
    config = response.json()
    assert set(config) == {"sites"}
    assert all(site["type"] == 4 and "jar" not in site for site in config["sites"])
    api = config["sites"][0]["api"]
    original = play(client, api).json()
    descriptor = client.get(response.links["urn:t4-sidecar:client-component"]["url"]).json()
    assert client.get(descriptor["spider"]).status_code == 410
    session = parse_qs(urlsplit(api).query)["t4_client"][0]
    assert acceleration.get(session).state == "disabled"
    assert play(client, api).json() == original
    assert client.get(descriptor["ready"].replace("/ready", "/component.json")).status_code == 410
    assert client.get("/t4/config.json").status_code == 200


def test_empty_profile_does_not_offer_unused_component(client):
    profile = client.post("/t4/api/profiles", json={
        "name": "Empty", "mode": "whitelist", "whitelist": [],
    }).json()["profile"]
    assert client.get(profile["subscription_path"]).json() == {"sites": []}
    assert client.get("/t4/api/client-acceleration").json()["pending"] == 0


def test_ready_client_gets_isolated_plan_and_exact_object_relay(client):
    first, proof = offer(client)
    second, other = offer(client)
    assert proof["session"] != other["session"]
    assert confirm(client, proof)
    enhanced = play(client, first["sites"][0]["api"]).json()
    assert enhanced["url"].startswith("proxy://")
    query = parse_qs(enhanced["url"].removeprefix("proxy://"))
    assert query["siteKey"] == ["t4_acceleration_" + proof["session"]]
    assert enhanced["header"] == {}
    plan = client.get(query["plan"][0])
    assert plan.status_code == 200
    assert plan.headers["cache-control"] == "no-store"
    assert plan.json()["direct"] == "https://cdn.example/film.mp4"
    relay_key = urlsplit(plan.json()["relay"]).path.rsplit("/", 1)[-1]
    asset = client.app.state.playback.get(relay_key)
    assert asset.url == plan.json()["direct"]
    assert asset.headers == plan.json()["headers"]
    assert asset.redirectable is False
    assert play(client, second["sites"][0]["api"]).json()["url"].startswith("https:")
    assert client.get("/t4/fm/env-at/bili/?play=demo").json()["url"].startswith("https:")
    assert "signed-agent" not in enhanced["url"]
    # Replacing the session in a plan URL cannot cross subscription sessions.
    assert client.get(query["plan"][0].replace(proof["session"], other["session"])).status_code == 410


def test_disable_reverts_to_plain_without_restarting_workers(client):
    config, properties = offer(client)
    assert confirm(client, properties)
    api = config["sites"][0]["api"]
    assert play(client, api).json()["url"].startswith("proxy://")
    client.post("/t4/acceleration/" + properties["session"] + "/disable")
    assert play(client, api).json()["url"].startswith("https://")
    assert not confirm(client, properties)
    assert client.get(f"/t4/acceleration/{properties['session']}/component.json").status_code == 410
    assert client.get(f"/t4/acceleration/{properties['session']}/client.jar").status_code == 410
    assert client.get("/t4/api/client-acceleration").json()["disabled"] == 1


def test_capabilities_are_profile_scoped_and_respect_changed_plugin_policy(client):
    profile = client.post("/t4/api/profiles", json={"name": "test"}).json()["profile"]
    config, properties = offer(client, profile["subscription_path"])
    assert confirm(client, properties)
    api = config["sites"][0]["api"]
    enhanced = play(client, api).json()
    plan_url = parse_qs(enhanced["url"][8:])["plan"][0]
    wrong_profile = "/t4/fm/env-at/bili/?play=x&t4_client=" + properties["session"]
    assert client.get(wrong_profile).json()["url"].startswith("https://")
    client.put("/t4/api/profiles/" + profile["id"], json={"name": "test", "enabled": False})
    assert client.get(plan_url).status_code == 410
    assert play(client, api).status_code == 404


def test_packaged_dex_matches_current_sources_and_does_not_bundle_host_abi(client):
    _, proof = offer(client)
    jar = client.get(f"/t4/acceleration/{proof['session']}/client.jar")
    with zipfile.ZipFile(io.BytesIO(jar.content)) as archive:
        manifest = json.loads(archive.read("t4-build.json"))
        root = Path(__file__).resolve().parents[2]
        assert manifest["sources"] == {
            item.relative_to(root).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in sorted((root / "client/fm").rglob("*.java"))
        }
        assert not any(name.endswith(".class") for name in archive.namelist())
        assert manifest["min_api"] == 21
        dex = archive.read("classes.dex")
        string_offset, = struct.unpack_from("<I", dex, 60)
        type_offset, = struct.unpack_from("<I", dex, 68)
        count, offset = struct.unpack_from("<II", dex, 96)
        defined = set()
        for index in range(count):
            type_index, = struct.unpack_from("<I", dex, offset + index * 32)
            string_index, = struct.unpack_from("<I", dex, type_offset + type_index * 4)
            start, = struct.unpack_from("<I", dex, string_offset + string_index * 4)
            while dex[start] & 0x80:
                start += 1
            start += 1
            defined.add(dex[start:dex.index(b"\0", start)].decode("utf-8"))
        assert "Lcom/github/catvod/spider/T4Relay;" in defined
        assert "Lcom/github/catvod/crawler/Spider;" not in defined
        assert "Lfi/iki/elonen/NanoHTTPD;" not in defined


@pytest.mark.parametrize("method,route", [
    ("GET", "fm"), ("JSON", "fm"), ("FORM", "fm"),
    ("GET", "module"), ("JSON", "module"), ("FORM", "module"),
])
@pytest.mark.parametrize("client_name", ["tvbox", "unknown", "ios", "web"])
def test_both_t4_route_shapes_use_confirmed_capability_not_brand(
    client, method, route, client_name,
):
    config, proof = offer(client, headers={"X-Client": client_name})
    assert confirm(client, proof)
    api = config["sites"][0]["api"] if route == "fm" else (
        "/t4/api/play?source=env-at&site=bili&t4_client=" + proof["session"]
    )
    if method == "GET":
        response = client.get(str(httpx.URL(api).copy_merge_params({"play": "demo"})),
                              headers={"X-Client": client_name})
    elif method == "JSON":
        response = client.post(api, json={"play": "demo"}, headers={"X-Client": client_name})
    else:
        response = client.post(api, data={"play": "demo"}, headers={"X-Client": client_name})
    assert response.status_code == 200
    assert response.json()["url"].startswith("proxy://")


def test_native_jar_is_optional_and_old_descriptor_respects_policy_changes(tmp_path):
    async def native_fetcher(_):
        return {"spider": "https://at.example/spring.jar", "sites": [
            {"key": "push", "name": "AT push", "api": "csp_Push", "type": 3,
             "jar": "https://at.example/native.jar", "ext": {"api": "https://at.example", "token": "private"}},
            {"key": "bili", "name": "Bili", "api": "csp_BiliBili"},
        ]}

    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
                     subscription_fetcher=native_fetcher, t4_adapter=MediaAdapter())
    with TestClient(app) as client:
        response = client.get("/t4/config.json")
        assert all(site["type"] == 4 for site in response.json()["sites"])
        assert "spring.jar" not in response.text
        assert "private" not in response.text
        url = response.links["urn:t4-sidecar:client-component"]["url"]
        descriptor = client.get(url).json()
        push = next(site for site in descriptor["sites"] if site["api"] == "csp_Push")
        assert push["jar"] == "https://at.example/native.jar"
        assert push["ext"] == {"api": "https://at.example", "token": "private"}
        assert descriptor["sites"][-1]["api"] == "csp_T4Relay"
        assert descriptor["sites"][-1]["hide"] == 1
        plugins = client.get("/t4/api/profiles/default/plugins").json()["plugins"]
        key = next(item["key"] for item in plugins if item["plugin_id"] == "push")
        client.put("/t4/api/profiles/default", json={
            "name": "default", "mode": "blacklist", "blacklist": [key],
        })
        assert not any(site["api"] == "csp_Push" for site in client.get(url).json()["sites"])


def test_optional_descriptor_cors_is_exposed_without_opening_management(client):
    origin = {"Origin": "https://player.example"}
    response = client.get("/t4/config.json", headers=origin)
    assert "link" in response.headers["access-control-expose-headers"].lower()
    url = response.links["urn:t4-sidecar:client-component"]["url"]
    descriptor = client.get(url, headers=origin)
    assert descriptor.headers["access-control-allow-origin"] == "*"
    assert descriptor.headers["cache-control"] == "no-store"
    assert client.get(descriptor.json()["spider"], headers=origin).headers[
        "access-control-allow-origin"
    ] == "*"
    assert "access-control-allow-origin" not in client.get(
        "/t4/api/profiles", headers=origin,
    ).headers


def test_optional_component_failure_after_ready_downgrades_same_api(client, monkeypatch):
    config, proof = offer(client)
    api = config["sites"][0]["api"]
    original = play(client, api).json()
    assert confirm(client, proof)
    enhanced = play(client, api).json()
    plan_url = parse_qs(enhanced["url"][8:])["plan"][0]

    def unavailable(identity):
        raise OSError("component no longer readable")

    monkeypatch.setattr(client.app.state.acceleration, "artifact", unavailable)
    assert client.get(f"/t4/acceleration/{proof['session']}/client.jar").status_code == 410
    assert play(client, api).json() == original
    assert client.get(plan_url).status_code == 410
    assert not confirm(client, proof)


def test_native_only_component_without_accelerator_keeps_standard_subscription(tmp_path):
    async def native_fetcher(_):
        return {"sites": [{
            "key": "push", "name": "AT push", "api": "csp_Push", "type": 3,
            "jar": "https://at.example/native.jar",
        }]}

    app = create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=native_fetcher, t4_adapter=MediaAdapter(),
    )
    app.state.acceleration.jar = tmp_path / "missing.jar"
    with TestClient(app) as client:
        profile = client.post("/t4/api/profiles", json={"name": "Native"}).json()["profile"]
        response = client.get(profile["subscription_path"])
        assert response.json() == {"sites": []}
        url = response.links["urn:t4-sidecar:client-component"]["url"]
        descriptor = client.get(url).json()
        assert "spider" not in descriptor
        assert "ready" not in descriptor
        assert descriptor["sites"][0]["api"] == "csp_Push"
        client.delete("/t4/api/profiles/" + profile["id"])
        assert client.get(url).status_code == 410
        assert client.get(profile["subscription_path"]).status_code == 404


def test_confirmed_default_session_does_not_enable_another_profile(client):
    config, proof = offer(client)
    assert confirm(client, proof)
    profile = client.post("/t4/api/profiles", json={"name": "Isolated"}).json()["profile"]
    api = client.get(profile["subscription_path"]).json()["sites"][0]["api"]
    assert play(client, api).json()["url"].startswith("https://")
    other_api = httpx.URL(api).copy_merge_params({"t4_client": proof["session"]})
    assert play(client, str(other_api)).json()["url"].startswith("https://")
    assert play(client, config["sites"][0]["api"]).json()["url"].startswith("proxy://")


def test_ready_client_plan_retention_is_bounded_and_policy_changes_revoke_new_plans(client):
    config, proof = offer(client)
    assert confirm(client, proof)
    plans = [parse_qs(play(client, config["sites"][0]["api"]).json()["url"][8:])["plan"][0]
             for _ in range(10)]
    assert client.get(plans[0]).status_code == 410
    assert client.get(plans[-1]).status_code == 200
    key = config["sites"][0]["key"]
    client.put("/t4/api/profiles/default", json={
        "name": "default", "mode": "blacklist", "blacklist": [key],
    })
    assert client.get(plans[-1]).status_code == 410
    assert play(client, config["sites"][0]["api"]).status_code == 403


def test_pending_expiry_capacity_and_missing_artifact_are_plain(tmp_path):
    now = [0.0]
    acceleration = ClientAcceleration(T4ProfileStore(tmp_path / "profiles.json"),
                                      capacity=2, clock=lambda: now[0], ttl=100, pending_ttl=10)
    first = acceleration.offer("http://nas.example", "default")
    second = acceleration.offer("http://nas.example", "default")
    assert acceleration.confirm(first.id, first.challenge, 1, "1")
    now[0] = 11
    assert acceleration.get(first.id).state == "ready"
    assert acceleration.get(second.id) is None
    acceleration.offer("http://nas.example", "default")
    acceleration.offer("http://nas.example", "default")
    assert acceleration.get(first.id) is None
    acceleration.jar = tmp_path / "missing.jar"
    assert acceleration.offer("http://nas.example", "default") is None
    now[0] = 200
    assert acceleration.prune() == 2
    assert acceleration.summary()["ready"] == 0


@pytest.mark.parametrize("changes", [
    {"url": "https://cdn.example/master.m3u8"},
    {"url": "https://cdn.example/no-extension"},
    {"url": "http://127.0.0.1/video.mp4"},
    {"url": "https://nas.example/t4/media/key.mp4"},
    {"header": {"Cookie": "private"}},
    {"header": {"Authorization": "private"}},
    {"format": "application/vnd.apple.mpegurl"},
    {"format": "application/dash+xml"},
    {"parse": 1}, {"playUrl": "https://parser.example/"},
])
def test_ineligible_media_keeps_original_contract(client, changes):
    config, proof = offer(client)
    assert confirm(client, proof)
    payload = {"url": "https://cdn.example/video.mp4", "parse": 0, "header": {}, **changes}
    acceleration = client.app.state.acceleration
    assert acceleration.wrap(
        proof["session"], payload, "https://nas.example", client.app.state.playback,
        profile="default", source="env-at", plugin="bili",
    ) is payload
