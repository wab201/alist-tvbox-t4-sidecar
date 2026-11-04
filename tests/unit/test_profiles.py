from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.at.subscription import SubscriptionParseError
from sidecar.gateway.config import Settings
from sidecar.gateway.fm import build_fm_path, build_site_key
from sidecar.gateway.main import create_app
from sidecar.gateway.profiles import PluginPolicyDenied, T4Profile, T4ProfileStore
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.adapter import T4Request
from tests.t4.test_navigation_search import SearchWorker, setup_adapter
from tests.unit.test_client_config import fetcher, source
from tests.unit.test_fm import FakeAdapter
from tests.unit.test_uploads import JAVASCRIPT_PLUGIN, PYTHON_PLUGIN


def test_profile_exports_are_independent_and_enforce_plugin_policy(tmp_path):
    adapter = FakeAdapter()
    app = create_app(
        Settings(plugin_dir=str(tmp_path)),
        SourceRegistry((source(),)),
        subscription_fetcher=fetcher,
        t4_adapter=adapter,
    )
    with TestClient(app, base_url="http://nas.test:8765") as client:
        available = client.get("/t4/api/profiles/default/plugins")
        assert available.status_code == 200
        plugins = available.json()["plugins"]
        bili = next(item for item in plugins if item["plugin_id"] == "bili")
        raw = next(item for item in plugins if item["plugin_id"] == "raw")

        created = client.post("/t4/api/profiles", json={
            "name": "客厅", "mode": "whitelist", "whitelist": [bili["key"]],
            "blacklist": [],
        })
        assert created.status_code == 201
        profile = created.json()["profile"]
        profile_id = profile["id"]
        assert profile["subscription_path"] == f"/t4/profiles/{profile_id}/config.json"

        config = client.get(
            profile["subscription_path"],
            headers={"Origin": "https://client.example"},
        )
        assert config.status_code == 200
        assert config.headers["access-control-allow-origin"] == "*"
        exported = config.json()
        sites = [site for site in exported["sites"] if site["type"] == 4]
        assert len(exported["sites"]) == 2
        assert exported["sites"][-1]["api"] == "csp_T4Relay"
        assert "/t4/acceleration/" in exported["spider"]
        assert exported["sites"][-1]["jar"] == exported["spider"]
        assert [site["key"] for site in sites] == [bili["key"]]
        assert all(site["api"].startswith(f"http://nas.test:8765/t4/profiles/{profile_id}/fm/") for site in sites)
        assert client.get("/t4/config.json").json()["sites"][0]["key"] == bili["key"]

        allowed = client.get(
            f"/t4/profiles/{profile_id}" + build_fm_path("env-at", "bili").removeprefix("/t4"),
            headers={"X-CLIENT": "ios"},
        )
        assert allowed.status_code == 200
        assert adapter.requests[-1].params["client"] == "ios"
        denied_path = f"/t4/profiles/{profile_id}" + build_fm_path("env-at", "raw").removeprefix("/t4")
        denied = client.get(denied_path)
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "plugin_disabled"

        restarted = create_app(
            Settings(plugin_dir=str(tmp_path)),
            SourceRegistry((source(),)),
            subscription_fetcher=fetcher,
            t4_adapter=FakeAdapter(),
        )
        with TestClient(restarted) as other:
            restored = other.get("/t4/api/profiles").json()["profiles"]
            assert {item["id"] for item in restored} == {"default", profile_id}
            assert other.get(profile["subscription_path"]).json()["sites"][0]["key"] == bili["key"]


def test_blacklist_and_disabled_profile_are_isolated(tmp_path):
    app = create_app(
        Settings(plugin_dir=str(tmp_path)),
        SourceRegistry((source(),)),
        subscription_fetcher=fetcher,
        t4_adapter=FakeAdapter(),
    )
    with TestClient(app) as client:
        raw_key = build_site_key("env-at", "raw")
        created = client.post("/t4/api/profiles", json={
            "name": "移动端", "mode": "blacklist", "blacklist": [raw_key],
        }).json()["profile"]
        profile_id = created["id"]
        sites = [site for site in client.get(created["subscription_path"]).json()["sites"]
                 if site["type"] == 4]
        assert raw_key not in {item["key"] for item in sites}
        assert len(sites) == 1

        disabled = client.put(f"/t4/api/profiles/{profile_id}", json={
            "name": "移动端", "enabled": False, "mode": "blacklist",
            "blacklist": [raw_key],
        })
        assert disabled.status_code == 200
        assert client.get(created["subscription_path"]).status_code == 404
        assert client.options(
            "/t4/config.json",
            headers={"Origin": "https://client.example", "Access-Control-Request-Method": "GET"},
        ).status_code == 200


@pytest.mark.parametrize("mode", ["all", "whitelist", "blacklist"])
@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
@pytest.mark.parametrize("module", ["home", "category", "search", "detail", "play", "action"])
def test_disabled_plugin_never_reaches_adapter(tmp_path, mode, transport, module):
    adapter = FakeAdapter()
    app = create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=fetcher, t4_adapter=adapter,
    )
    key = build_site_key("env-at", "raw")
    with TestClient(app) as client:
        profile = client.post("/t4/api/profiles", json={
            "name": "Policy", "mode": mode, "whitelist": [key], "blacklist": [key],
        }).json()["profile"]
        url = f"/t4/profiles/{profile['id']}/fm/env-at/raw/api/{module}"
        if transport == "GET":
            response = client.get(url)
        elif transport == "JSON":
            response = client.post(url, json={})
        else:
            response = client.post(url, data={"pg": "1"})
        assert response.status_code == 403
        assert not adapter.requests
        assert client.get(f"/t4/profiles/{profile['id']}/api/{module}",
                          params={"source": "env-at", "site": "raw"}).status_code == 403


def test_default_policy_cannot_be_replaced_by_query_or_body(tmp_path):
    adapter = FakeAdapter()
    with TestClient(create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=fetcher, t4_adapter=adapter,
    )) as client:
        other = client.post("/t4/api/profiles", json={"name": "Unrestricted"}).json()["profile"]
        assert client.put("/t4/api/profiles/default", json={
            "name": "Default", "mode": "blacklist",
            "blacklist": [build_site_key("env-at", "raw")],
        }).status_code == 200
        assert client.get("/t4/fm/env-at/raw/", params={
            "profile_id": other["id"],
        }).status_code == 403
        assert client.post("/t4/api/home", json={
            "profile_id": other["id"], "source": "env-at", "site": "raw",
        }).status_code == 403
        assert client.get("/t4/config.json", params={
            "profile_id": other["id"],
        }).json()["sites"][0]["name"] == "BiliBili"
        assert len([site for site in client.get(other["subscription_path"]).json()["sites"]
                    if site["type"] == 4]) == 2
        assert not adapter.requests


def test_profile_crud_and_failed_persistence_do_not_damage_other_profiles(tmp_path, monkeypatch):
    store = T4ProfileStore(tmp_path / "profiles.json")
    with ThreadPoolExecutor(max_workers=4) as executor:
        created = list(executor.map(lambda i: store.save(None, {"name": f"P{i}"}), range(12)))
    assert len(T4ProfileStore(store.path).all()) == 13
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save(created[0].id, {"name": " ", "mode": "all"})
    with pytest.raises(ValueError):
        store.delete("default")
    assert store.path.read_bytes() == before

    def failed(*args):
        raise OSError("read only")

    monkeypatch.setattr(store, "_persist", failed)
    with pytest.raises(OSError):
        store.save(created[0].id, {"name": "Changed"})
    assert store.get(created[0].id).name == "P0"


def test_same_names_and_plugin_ids_in_different_sources_stay_independent(tmp_path):
    async def same(_):
        return {"sites": [{"key": "bili", "name": "Same", "api": "csp_BiliBili"}]}

    with TestClient(create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source("one"), source("two"))),
        subscription_fetcher=same, t4_adapter=FakeAdapter(),
    )) as client:
        profile = client.post("/t4/api/profiles", json={
            "name": "One", "mode": "whitelist",
            "whitelist": [build_site_key("one", "bili")],
        }).json()["profile"]
        sites = [site for site in client.get(profile["subscription_path"]).json()["sites"]
                 if site["type"] == 4]
        assert len(sites) == 1 and "/one/bili/" in sites[0]["api"]
        assert len(client.get("/t4/api/profiles/default/plugins").json()["plugins"]) == 2
        assert client.delete(f"/t4/api/profiles/{profile['id']}").status_code == 200
        assert client.get(profile["subscription_path"]).status_code == 404
        assert len([site for site in client.get("/t4/config.json").json()["sites"]
                    if site["type"] == 4]) == 2


def test_navigation_filters_delegates_and_separates_profile_sessions(tmp_path):
    async def run():
        worker = SearchWorker()
        adapter, _ = setup_adapter(tmp_path, worker)
        allowed = (build_site_key("source-a", "nav"), build_site_key("source-a", "p1"))
        first = T4Profile(id="first", mode="whitelist", whitelist=allowed)
        second = replace(first, id="second")
        request = T4Request("search", "source-a", "nav", {
            "wd": "Film", "nav_search": True, "nav_incremental": True,
        }, profile=first)
        try:
            result = await adapter.dispatch(request)
            assert result["t4_search"]["total"] == 1
            assert all(row["t4_source"] == "Source 1" for row in result["list"])
            assert {p for p, _, _ in worker.calls} == {"p1"}
            session = result["t4_search"]["session"]
            with pytest.raises(SubscriptionParseError, match="会话已过期"):
                await adapter.dispatch(replace(request, profile=second, params={
                    **request.params, "nav_session": session,
                }))
            other = await adapter.dispatch(replace(request, profile=second))
            assert other["t4_search"]["session"] != session
            row = result["list"][0]
            blocked = replace(first, blacklist=(build_site_key("source-a", "p1"),))
            worker.calls.clear()
            with pytest.raises(PluginPolicyDenied):
                await adapter.dispatch(T4Request(
                    "detail", "source-a", "nav", {"ids": row["vod_id"]}, profile=blocked,
                ))
            assert worker.calls == []
        finally:
            await asyncio.gather(*adapter._navigation_tasks)
            adapter.close()
    asyncio.run(run())


@pytest.mark.parametrize("origin", ["https://web.example", "http://mobile.example:9000"])
def test_client_cors_json_preflight_and_management_are_separate(tmp_path, origin):
    adapter = FakeAdapter()
    with TestClient(create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=fetcher, t4_adapter=adapter,
    )) as client:
        profile = client.post("/t4/api/profiles", json={"name": "Web"}).json()["profile"]
        url = f"/t4/profiles/{profile['id']}/fm/env-at/bili/"
        headers = {"Origin": origin, "Access-Control-Request-Method": "POST",
                   "Access-Control-Request-Headers": "content-type,x-client"}
        preflight = client.options(url, headers=headers)
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "*"
        for name in ("android", "ios", "tv"):
            response = client.post(url, json={"wd": "Film"},
                                   headers={"Origin": origin, "X-CLIENT": name})
            assert response.status_code == 200
            assert adapter.requests[-1].params["client"] == name
            assert adapter.requests[-1].profile.id == profile["id"]
        admin = client.options("/t4/api/profiles", headers=headers)
        assert "access-control-allow-origin" not in admin.headers


@pytest.mark.parametrize("language,content", [("py", PYTHON_PLUGIN), ("js", JAVASCRIPT_PLUGIN)])
@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_profile_full_chain_uses_actual_uploaded_runtime(tmp_path, language, content, transport):
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())) as client:
        plugin = client.post("/t4/api/plugins/upload", files={
            "file": (f"profile.{language}", content),
        }).json()["plugin"]
        profile = client.post("/t4/api/profiles", json={
            "name": "Runtime", "mode": "whitelist",
            "whitelist": [build_site_key("local-uploaded", plugin["id"])],
        }).json()["profile"]
        site = client.get(profile["subscription_path"]).json()["sites"][0]

        def call(params):
            url = (
                str(httpx.URL(site["api"]).copy_merge_params(params))
                if transport == "GET" else site["api"]
            )
            options = {} if transport == "GET" else {
                "json" if transport == "JSON" else "data": params,
            }
            response = client.request("GET" if transport == "GET" else "POST",
                                      url, **options)
            assert response.status_code == 200, response.text
            return response.json()

        home = call({})
        assert call({"t": "all", "pg": 2})["page"] == 2
        assert call({"wd": "demo", "pg": 3})["page"] == 3
        detail = call({"ids": home["list"][0]["vod_id"]})["list"][0]
        play_id = detail["vod_play_url"].split("$", 1)[1]
        assert call({"play": play_id, "flag": "Uploaded"})["parse"] == 0
        assert call({"ac": "action", "action": "inspect"})["action"] == "inspect"
        assert client.put(f"/t4/api/profiles/{profile['id']}", json={
            "name": "Runtime", "mode": "whitelist", "whitelist": [],
        }).status_code == 200
        assert client.get(site["api"]).status_code == 403
        assert client.get(profile["subscription_path"]).json()["sites"] == []


def test_two_profiles_reuse_same_worker_and_client_headers_are_request_local(tmp_path):
    content = b"""
import os
from base.spider import Spider as BaseSpider
class Spider(BaseSpider):
    def init(self, extend): self.calls = 0
    def homeContent(self, filter):
        self.calls += 1
        return {"list": [], "pid": os.getpid(), "calls": self.calls}
"""
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())) as client:
        client.post("/t4/api/plugins/upload", files={"file": ("shared.py", content)})
        profiles = [client.post("/t4/api/profiles", json={"name": name}).json()["profile"]
                    for name in ("TV", "Mobile")]
        apis = [client.get(p["subscription_path"]).json()["sites"][0]["api"] for p in profiles]
        first = client.get(apis[0]).json()
        second = client.get(apis[1]).json()
        assert first["pid"] == second["pid"]
        assert (first["calls"], second["calls"]) == (1, 2)

    adapter = FakeAdapter()
    with TestClient(create_app(
        Settings(plugin_dir=str(tmp_path / "headers")), SourceRegistry((source(),)),
        t4_adapter=adapter,
    )) as client:
        profile = client.post("/t4/api/profiles", json={"name": "Concurrent"}).json()["profile"]
        url = f"/t4/profiles/{profile['id']}/fm/env-at/bili/"
        names = ["tv", "android", "ios", "web"] * 3
        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(
                lambda name: client.post(url, json={"wd": name}, headers={"X-CLIENT": name}), names,
            ))
        assert all(r.status_code == 200 for r in responses)
        assert len(adapter.requests) == len(names)
        assert all(r.params["client"] == r.params["wd"] for r in adapter.requests)


def test_default_disable_and_custom_delete_block_old_routes_without_affecting_other_profiles(tmp_path):
    adapter = FakeAdapter()
    with TestClient(create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source(),)),
        subscription_fetcher=fetcher, t4_adapter=adapter,
    )) as client:
        profile = client.post("/t4/api/profiles", json={"name": "TV"}).json()["profile"]
        old_api = client.get(profile["subscription_path"]).json()["sites"][0]["api"]
        renamed = client.put(f"/t4/api/profiles/{profile['id']}", json={
            "name": "Renamed TV",
        }).json()["profile"]
        assert renamed["subscription_path"] == profile["subscription_path"]
        assert client.get(old_api).status_code == 200
        assert client.put("/t4/api/profiles/default", json={
            "name": "Default", "enabled": False,
        }).status_code == 200
        for path in ("/t4/config.json", "/t4/fm/env-at/bili/",
                     "/t4/api/home?source=env-at&site=bili"):
            assert client.get(path).status_code == 404
        assert client.get(old_api).status_code == 200
        assert client.delete(f"/t4/api/profiles/{profile['id']}").status_code == 200
        adapter.requests.clear()
        assert client.get(profile["subscription_path"]).status_code == 404
        assert client.get(old_api).status_code == 404
        assert not adapter.requests


def test_navigation_play_and_policy_change_cannot_reuse_blocked_result(tmp_path):
    async def run():
        worker = SearchWorker()
        adapter, _ = setup_adapter(tmp_path, worker)
        policy = T4Profile(id="tv", blacklist=(build_site_key("source-a", "p8"),))
        request = T4Request("search", "source-a", "nav", {
            "wd": "Film", "nav_search": True, "nav_incremental": True,
        }, profile=policy)
        try:
            result = await adapter.dispatch(request)
            detail = await adapter.dispatch(replace(
                request, module="detail", params={"ids": result["list"][0]["vod_id"]},
            ))
            play_id = detail["list"][0]["vod_play_url"].split("$$$")[1].partition("$")[2]
            changed = replace(policy, blacklist=(
                *policy.blacklist, build_site_key("source-a", "p1"),
            ))
            worker.calls.clear()
            with pytest.raises(PluginPolicyDenied):
                await adapter.dispatch(replace(
                    request, module="play", params={"play": play_id}, profile=changed,
                ))
            assert worker.calls == []
            changed = replace(policy, blacklist=())
            with pytest.raises(SubscriptionParseError, match="会话已过期"):
                await adapter.dispatch(replace(
                    request, profile=changed,
                    params={**request.params, "nav_session": result["t4_search"]["session"]},
                ))
        finally:
            await asyncio.gather(*adapter._navigation_tasks)
            adapter.close()
    asyncio.run(run())
