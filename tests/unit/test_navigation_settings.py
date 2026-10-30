import asyncio
from dataclasses import replace
import threading

from fastapi.testclient import TestClient
import pytest

from sidecar.at.subscription import SubscriptionSnapshot
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.navigation_settings import NavigationSettingsStore, can_search
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.t4.adapter import T4Adapter, T4Request
from tests.t4.test_adapter import descriptor, source


def plugins():
    base = descriptor()
    return tuple(replace(base, plugin_id=f"p{i}", name=f"Source {i}") for i in range(9))


def test_selection_is_atomic_persistent_and_copy_safe(tmp_path, monkeypatch):
    path = tmp_path / "navigation.json"
    settings = NavigationSettingsStore(path)
    assert settings.selection("a") is None
    settings.update("a", ["p8", "p1", "p8"])
    selected = settings.selection("a")
    selected.clear()
    assert NavigationSettingsStore(path).selection("a") == ["p8", "p1"]
    monkeypatch.setattr(type(path), "replace", lambda *args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        settings.update("a", [])
    assert settings.selection("a") == ["p8", "p1"]
    assert NavigationSettingsStore(path).selection("a") == ["p8", "p1"]


def test_selection_api_lists_only_eligible_sources_and_keeps_ids(tmp_path):
    async def fetcher(_):
        return {"sites": [
            {"key": f"p{i}", "name": "Same name", "api": "csp_PyProxy",
             "searchable": i != 0, "ext": {"raw": True, "source": "https://at.example/demo.py"}}
            for i in range(9)
        ] + [{"key": "nav", "api": "csp_PianDan", "ext": {"api": "https://at.example", "token": "private"}}]}

    path = "/t4/api/sources/source-a/navigation"
    settings = Settings(plugin_dir=str(tmp_path))
    for restart in range(2):
        with TestClient(create_app(settings, SourceRegistry((source(),)),
                                   subscription_fetcher=fetcher)) as client:
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            data = response.json()
            assert [p["id"] for p in data["plugins"]] == [f"p{i}" for i in range(1, 9)]
            assert "private" not in response.text and "https://" not in response.text
            if restart:
                assert [p["id"] for p in data["plugins"] if p["selected"]] == ["p8"]
                assert client.put(path, json={"plugin_ids": []}).json()["plugins"][0]["selected"] is False
                assert client.put(path, json={"plugin_ids": None}).json()["plugin_ids"] is None
            else:
                assert data["plugin_ids"] is None and all(p["selected"] for p in data["plugins"])
                assert client.put(path, json={"plugin_ids": ["p8"]}).status_code == 200
                for invalid in (["p0"], ["nav"], ["missing"]):
                    assert client.put(path, json={"plugin_ids": invalid}).status_code == 400
                assert client.get(path).json()["plugin_ids"] == ["p8"]
            assert client.get("/t4/api/sources/missing/navigation").status_code == 404


@pytest.mark.parametrize("value", [0, "0", False])
def test_nonsearchable_plugins_are_ineligible(value):
    assert not can_search(replace(descriptor(), site_options={"searchable": value}))
    assert not can_search(replace(descriptor(), api="csp_PianDan"))
    assert not can_search(replace(descriptor(), api="csp_Push"))


@pytest.mark.parametrize("selection,expected", [
    (None, list(range(9))), (["p8", "p1"], [1, 8]), ([], []),
])
def test_navigation_no_longer_truncates_to_six_and_respects_selection(tmp_path, selection, expected):
    calls = []

    class Worker:
        def call(self, key, plugin, method, params):
            calls.append((plugin.plugin_id, method))
            if method == "search":
                return {"list": [{"vod_id": "actual", "vod_name": "Film", "vod_year": "2026"}]}
            return {"list": [{"vod_id": "actual", "vod_play_from": "HD",
                             "vod_play_url": "Episode$actual-play"}]}

    nav = replace(descriptor(), plugin_id="nav", api="csp_PianDan")
    store = NavigationSettingsStore(tmp_path / "navigation.json")
    store.update("source-a", selection)
    adapter = T4Adapter(
        SourceRegistry((source(),)), Worker(), navigation_settings=store,
        local_snapshot_provider=lambda _: SubscriptionSnapshot("source-a", 9, plugins()),
    )
    adapter._navigation_cards[("source-a", "nav", "film")] = {
        "vod_name": "Film", "vod_year": "2026"}
    request = T4Request("detail", "source-a", "nav", {"ids": "film"})
    if not expected:
        with pytest.raises(ValueError, match="范围为空"):
            asyncio.run(adapter._navigation_detail(
                request, source(), nav, SubscriptionSnapshot("source-a", 10, plugins())))
        assert not calls
    else:
        result = asyncio.run(adapter._navigation_detail(
            request, source(), nav, SubscriptionSnapshot("source-a", 10, plugins())))
        assert result["list"][0]["vod_play_from"].split("$$$") == [
            f"Source {i} · HD" for i in expected]
        assert sorted(p for p, method in calls if method == "search") == sorted(
            f"p{i}" for i in expected)
        assert not any(method == "play" for _, method in calls)
    adapter.close()


def test_budget_returns_partial_results_without_releasing_active_backend_slots(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Worker:
        def call(self, key, plugin, method, params):
            calls.append((plugin.plugin_id, method))
            if method == "search" and plugin.plugin_id in {"p0", "p1", "p2"}:
                entered.set()
                release.wait(3)
            if method == "search":
                return {"list": [{"vod_id": "film", "vod_name": "Film"}]}
            return {"list": [{"vod_id": "film", "vod_play_from": "HD",
                             "vod_play_url": "Episode$play"}]}

    async def run():
        nav = replace(descriptor(), plugin_id="nav", api="csp_PianDan")
        adapter = T4Adapter(
            SourceRegistry((source(),)), Worker(),
            local_snapshot_provider=lambda _: SubscriptionSnapshot("source-a", 9, plugins()),
        )
        adapter._navigation_budget = 0.08
        adapter._navigation_cards[("source-a", "nav", "film")] = {"vod_name": "Film"}
        try:
            with pytest.raises(ValueError, match="时限"):
                await adapter._navigation_detail(
                    T4Request("detail", "source-a", "nav", {"ids": "film"}),
                    source(), nav, SubscriptionSnapshot("source-a", 9, plugins()))
            assert entered.is_set()
            assert adapter._navigation_slots.locked()
            assert len(calls) == 3
        finally:
            release.set()
            await asyncio.gather(*adapter._navigation_tasks)
            assert len(calls) == 3  # No queued searches or late detail requests.
            assert not adapter._navigation_slots.locked()
            adapter.close()

    asyncio.run(run())
