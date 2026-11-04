import io
import hashlib
import json
from pathlib import Path
import struct
from urllib.parse import parse_qs, urlsplit
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.client_acceleration import ClientAcceleration, can_probe
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


def offer(client, path="/t4/config.json?client=fm"):
    response = client.get(path, headers={"User-Agent": "okhttp/4.12.0"})
    assert response.status_code == 200
    config = response.json()
    assert config["sites"][-1]["hide"] == 1
    assert config["sites"][-1]["searchable"] == 0
    jar = client.get(config["spider"])
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


@pytest.mark.parametrize("agent,client_name,mode,expected", [
    ("okhttp/4.12", "", "auto", False),
    ("Dalvik Android", "", "auto", False),
    ("FongMi/4", "", "auto", True),
    ("okhttp/4.12", "fongmi", "auto", True),
    ("okhttp/4.12", "ios", "fm", False),
    ("okhttp/4.12", "web", "fm", False),
    ("Mozilla/5.0 (iPhone)", "", "fm", False),
    ("Mozilla/5.0 (Linux; Android)", "", "fm", False),
    ("Infuse", "", "fm", False),
    ("trim.media", "", "fm", False),
    ("okhttp/4.12", "", "fm", True),
    ("FongMi", "", "pure", False),
])
def test_probe_hints_are_not_capability_evidence(agent, client_name, mode, expected):
    assert can_probe({"user-agent": agent, "x-client": client_name}, mode) is expected


def test_unknown_and_non_android_clients_keep_original_subscription(client):
    for headers in ({}, {"User-Agent": "okhttp/4.12"},
                    {"User-Agent": "Mozilla/5.0 (iPhone)"}, {"X-CLIENT": "fnmedia"}):
        response = client.get("/t4/config.json", headers=headers)
        assert "spider" not in response.json()
        assert all(site["type"] == 4 for site in response.json()["sites"])
        assert "t4_client" not in response.text
    branded = client.get("/t4/config.json", headers={"User-Agent": "FongMi"})
    assert "spider" in branded.json()
    pure = client.get("/t4/config.json?client=pure", headers={"User-Agent": "FongMi"})
    assert "spider" not in pure.json()
    assert branded.headers["cache-control"] == "no-store"
    assert "User-Agent" in branded.headers["vary"]


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
    copied = client.get(
        str(httpx.URL(first["sites"][0]["api"]).copy_merge_params({"play": "demo"})),
        headers={"X-CLIENT": "ios"},
    )
    assert copied.json()["url"].startswith("https://")
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
    assert client.get("/t4/api/client-acceleration").json()["disabled"] == 1


def test_capabilities_are_profile_scoped_and_respect_changed_plugin_policy(client):
    profile = client.post("/t4/api/profiles", json={"name": "test"}).json()["profile"]
    config, properties = offer(client, profile["subscription_path"] + "?client=fm")
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
    config, _ = offer(client)
    with zipfile.ZipFile(io.BytesIO(client.get(config["spider"]).content)) as archive:
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
def test_both_t4_route_shapes_support_confirmed_client_and_all_transports(client, method, route):
    config, proof = offer(client)
    assert confirm(client, proof)
    api = config["sites"][0]["api"] if route == "fm" else (
        "/t4/api/play?source=env-at&site=bili&t4_client=" + proof["session"]
    )
    if method == "GET":
        response = play(client, api)
    elif method == "JSON":
        response = client.post(api, json={"play": "demo"})
    else:
        response = client.post(api, data={"play": "demo"})
    assert response.status_code == 200
    assert response.json()["url"].startswith("proxy://")


def test_native_jar_is_unchanged_and_helper_is_not_a_user_source(tmp_path):
    async def native_fetcher(_):
        return {"spider": "https://at.example/spring.jar", "sites": [
            {"key": "push", "name": "AT push", "api": "csp_Push", "type": 3,
             "jar": "https://at.example/native.jar", "ext": {"api": "https://at.example", "token": "private"}},
            {"key": "bili", "name": "Bili", "api": "csp_BiliBili"},
        ]}

    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
                     subscription_fetcher=native_fetcher, t4_adapter=MediaAdapter())
    with TestClient(app) as client:
        ordinary = client.get("/t4/config.json").json()["sites"]
        enhanced = client.get("/t4/config.json?client=fm").json()
        push = next(site for site in ordinary if site["type"] == 3)
        assert next(site for site in enhanced["sites"] if site["key"] == push["key"]) == push
        assert enhanced["sites"][-1]["api"] == "csp_T4Relay"
        assert enhanced["sites"][-1]["hide"] == 1
        assert all(site["type"] == 4 for site in enhanced["sites"]
                   if site["key"] != push["key"] and not site.get("hide"))


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
