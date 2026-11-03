import asyncio
from dataclasses import replace
import json
import threading
import time

from fastapi.testclient import TestClient
import pytest

from sidecar.at.subscription import SubscriptionSnapshot
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.navigation_settings import NavigationSettingsStore
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.t4.adapter import T4Adapter, T4Request
from sidecar.t4.opaque import OpaqueIdStore
from tests.t4.test_adapter import descriptor, source


class SearchWorker:
    def __init__(self):
        self.calls = []
        self.empty = False
        self.broken = set()

    def call(self, key, plugin, method, params):
        self.calls.append((plugin.plugin_id, method, params))
        if plugin.plugin_id in self.broken:
            raise RuntimeError("upstream unavailable")
        if method == "home":
            return {"list": [
                {"vod_id": "tmdb:1", "vod_name": "Film", "type_name": "电影"},
                {"vod_id": "folder", "vod_name": "Catalog", "vod_tag": "folder"},
            ]}
        if method == "search":
            assert params["key"] == "Film"
            rows = [
                {"vod_id": "same-id", "vod_name": "Film", "vod_year": "2026"},
                {"vod_id": "old", "vod_name": "Film", "vod_year": "2025"},
                {"vod_id": "folder", "vod_name": "Film resources", "vod_tag": "folder"},
            ]
            return {"list": [] if self.empty else rows}
        if method == "category":
            assert params["tid"] == "folder"
            return {"list": [{"vod_id": "same-id", "vod_name": "Film"}]}
        if method == "detail":
            assert params["ids"] == ["same-id"]
            return {"list": [{
                "vod_id": "same-id", "vod_name": "Film", "type_name": "电视剧",
                "vod_play_from": "HD$$$SD",
                "vod_play_url": "第1集$a1#第2集$a2$$$第2集$b2",
            }]}
        if method == "play":
            assert params["id"] == "b2" and params["flag"] == "SD"
            return {"url": "https://media.example/episode2.mp4", "parse": 0}
        raise AssertionError(method)


def setup_adapter(tmp_path, worker, *, path=None):
    nav = replace(descriptor(), plugin_id="nav", api="csp_PianDan", name="Navigation")
    targets = tuple(replace(descriptor(), plugin_id=f"p{i}", name=f"Source {i}")
                    for i in range(9))
    snapshot = SubscriptionSnapshot("source-a", 10, (nav, *targets))
    settings = NavigationSettingsStore(tmp_path / "navigation.json")
    settings.update("source-a", ["p1", "p8"])
    adapter = T4Adapter(
        SourceRegistry((source(),)), worker, navigation_settings=settings,
        opaque_ids=OpaqueIdStore(path=path), local_snapshot_provider=lambda _: snapshot,
    )
    return adapter, settings


@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_navigation_search_is_lazy_selected_and_delegates_folder_detail_play(tmp_path, transport):
    worker = SearchWorker()
    adapter, _ = setup_adapter(tmp_path, worker)
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)),
                               SourceRegistry((source(),)), t4_adapter=adapter)) as client:
        url = "/t4/fm/source-a/nav/"

        def call(params):
            if transport == "GET":
                return client.get(url, params=params)
            if transport == "JSON":
                return client.post(url, json=params)
            return client.post(url, data=params)

        home = call({}).json()["list"]
        assert home[0]["t4_action"] == {"type": "search", "keyword": "Film"}
        assert "t4_action" not in home[1]
        worker.calls.clear()
        result = call({"wd": "Film", "nav_search": "true"}).json()
        assert result["t4_search"] == {
            "completed": 2, "failed": 0, "total": 2, "partial": False, "truncated": 0}
        rows = result["list"]
        assert len(rows) == 6
        assert [p for p, method, _ in worker.calls] == ["p1", "p8"]
        assert all(method == "search" for _, method, _ in worker.calls)
        assert {row["t4_source"] for row in rows} == {"Source 1", "Source 8"}
        other = next(r for r in rows if r["t4_source"] == "Source 8" and r["vod_year"] == "2026")
        assert rows[0]["vod_id"] != other["vod_id"]
        assert rows[0]["content_key"] != other["content_key"]
        assert "private-" not in json.dumps(result)
        assert all("t4_action" not in row for row in rows)
        folder = next(r for r in rows if r.get("vod_tag") == "folder" and r["t4_source"] == "Source 1")
        nested = call({"ac": "detail", "t": folder["vod_id"]}).json()["list"][0]
        assert nested["content_key"] == rows[0]["content_key"]
        detail = call({"ids": rows[0]["vod_id"]}).json()["list"][0]
        assert detail["vod_id"] == rows[0]["vod_id"]
        assert detail["content_key"] == rows[0]["content_key"]
        assert detail["vod_play_from"] == "HD$$$SD"
        assert not any(method == "play" for _, method, _ in worker.calls)
        play_id = detail["vod_play_url"].split("$$$")[1].partition("$")[2]
        play = call({"play": play_id, "flag": "ignored"})
        assert play.status_code == 200
        assert worker.calls[-1][0] == "p1"
        assert worker.calls[-1][2]["id"] == "b2"
    adapter.close()


def test_search_result_identity_and_target_survive_restart(tmp_path):
    worker = SearchWorker()
    path = tmp_path / "ids.sqlite"
    before, _ = setup_adapter(tmp_path, worker, path=path)
    row = asyncio.run(before.dispatch(T4Request(
        "search", "source-a", "nav", {"wd": "Film", "nav_search": True})))["list"][0]
    before.close()
    after, _ = setup_adapter(tmp_path, worker, path=path)
    try:
        worker.calls.clear()
        detail = asyncio.run(after.dispatch(T4Request(
            "detail", "source-a", "nav", {"ids": row["vod_id"]})))["list"][0]
        assert detail["vod_id"] == row["vod_id"]
        assert detail["content_key"] == row["content_key"]
        assert [method for _, method, _ in worker.calls] == ["detail"]
    finally:
        after.close()


def test_no_results_and_failures_are_distinct_and_empty_selection_is_honest(tmp_path):
    worker = SearchWorker()
    adapter, settings = setup_adapter(tmp_path, worker)
    request = T4Request("search", "source-a", "nav", {"wd": "Film", "nav_search": True})
    try:
        worker.empty = True
        empty = asyncio.run(adapter.dispatch(request))
        assert empty["list"] == [] and empty["t4_search"]["completed"] == 2
        worker.broken = {"p1"}
        with pytest.raises(ValueError, match="失败或超时"):
            asyncio.run(adapter.dispatch(request))
        worker.broken = {"p1", "p8"}
        with pytest.raises(ValueError, match="失败或超时"):
            asyncio.run(adapter.dispatch(request))
        settings.update("source-a", [])
        with pytest.raises(ValueError, match="范围为空"):
            asyncio.run(adapter.dispatch(request))
    finally:
        adapter.close()


def test_search_ranks_exact_title_and_year_but_preserves_user_choice(tmp_path):
    class Ranked(SearchWorker):
        def call(self, key, plugin, method, params):
            assert method == "search"
            return {"list": [
                {"vod_id": "clip", "vod_name": "Film highlights", "vod_year": "2026"},
                {"vod_id": "old", "vod_name": "Film", "vod_year": "2025"},
                {"vod_id": "new", "vod_name": "Film", "vod_year": "2026 / CN"},
            ]}

    adapter, _ = setup_adapter(tmp_path, Ranked())
    try:
        result = asyncio.run(adapter.dispatch(T4Request(
            "search", "source-a", "nav", {"wd": "Film", "nav_search": True, "year": "2026"})))
        assert [row["vod_year"] for row in result["list"][:2]] == ["2026 / CN"] * 2
        assert len(result["list"]) == 6  # Related titles are choices, never auto-bound.
    finally:
        adapter.close()


def test_search_caps_large_responses_and_reports_truncation(tmp_path):
    class Large(SearchWorker):
        def call(self, key, plugin, method, params):
            return {"list": [{"vod_id": str(i), "vod_name": "Film " + str(i),
                              "vod_content": "x" * 20000} for i in range(200)]}

    adapter, _ = setup_adapter(tmp_path, Large())
    try:
        result = asyncio.run(adapter.dispatch(T4Request(
            "search", "source-a", "nav", {"wd": "Film", "nav_search": True})))
        assert 0 < len(result["list"]) < 100
        assert result["t4_search"]["truncated"] == 400 - len(result["list"])
        assert len(json.dumps(result).encode()) < 800 * 1024
    finally:
        adapter.close()


def test_budget_keeps_active_slots_and_does_not_start_late_work(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow(SearchWorker):
        def call(self, key, plugin, method, params):
            if plugin.plugin_id == "p1":
                entered.set()
                release.wait(3)
            return super().call(key, plugin, method, params)

    async def run():
        worker = Slow()
        adapter, _ = setup_adapter(tmp_path, worker)
        adapter._navigation_budget = 0.05
        try:
            result = await adapter.dispatch(T4Request(
                "search", "source-a", "nav", {"wd": "Film", "nav_search": True}))
            assert entered.is_set() and result["t4_search"]["partial"]
            assert result["t4_search"]["completed"] == 1
            assert all(row["t4_source"] == "Source 8" for row in result["list"])
        finally:
            release.set()
            await asyncio.gather(*adapter._navigation_tasks)
            assert all(method == "search" for _, method, _ in worker.calls)
            assert adapter._navigation_slots._value == 3
            adapter.close()

    asyncio.run(run())


def test_incremental_first_window_does_not_wait_for_slow_source_and_is_append_only(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Progressive(SearchWorker):
        def call(self, key, plugin, method, params):
            assert method == "search"
            self.calls.append((plugin.plugin_id, method, params))
            if plugin.plugin_id == "p1":
                entered.set()
                release.wait(4)
            return {"list": [
                {"vod_id": str(i), "vod_name": "Film", "vod_year": "2026"}
                for i in range(35)]}

    async def run():
        worker = Progressive()
        adapter, _ = setup_adapter(tmp_path, worker)
        request = T4Request("search", "source-a", "nav", {
            "wd": "Film", "nav_search": True, "nav_incremental": True,
            "nav_offset": 0, "nav_limit": 22})
        try:
            start = time.monotonic()
            first = await adapter.dispatch(request)
            assert time.monotonic() - start < 1
            assert entered.is_set()
            assert first["t4_search"]["pending"]
            assert first["t4_search"]["completed"] == 1
            assert len(first["list"]) == 22
            assert all(row["t4_source"] == "Source 8" for row in first["list"])
            session = first["t4_search"]["session"]
            release.set()
            await asyncio.gather(*adapter._navigation_tasks)
            pages = await asyncio.gather(*(adapter.dispatch(replace(request, params={
                **request.params, "nav_session": session, "nav_offset": offset}))
                for offset in (0, 22, 44, 66)))
            assert pages[0]["list"] == first["list"]
            assert [len(p["list"]) for p in pages] == [22, 22, 22, 4]
            assert all(p["total"] == 70 for p in pages)
            assert not any(p["t4_search"]["pending"] for p in pages)
            ids = [r["content_key"] for page in pages for r in page["list"]]
            assert len(set(ids)) == 70
            assert len(worker.calls) == 2
            assert adapter._navigation_slots._value == 3
        finally:
            release.set()
            await asyncio.gather(*adapter._navigation_tasks)
            adapter.close()

    asyncio.run(run())


def test_incremental_budget_freezes_results_and_keeps_worker_slot(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow(SearchWorker):
        def call(self, key, plugin, method, params):
            if plugin.plugin_id == "p1":
                entered.set()
                release.wait(4)
                raise RuntimeError("late failure")
            return super().call(key, plugin, method, params)

    async def run():
        adapter, _ = setup_adapter(tmp_path, Slow())
        adapter._navigation_budget = 0.05
        request = T4Request("search", "source-a", "nav", {
            "wd": "Film", "nav_search": True, "nav_incremental": True})
        try:
            result = await adapter.dispatch(request)
            assert entered.is_set() and result["t4_search"]["partial"]
            assert not result["t4_search"]["pending"]
            assert result["t4_search"]["completed"] == 1
            assert result["t4_search"]["failed"] == 0
            assert adapter._navigation_slots._value == 2
            release.set()
            await asyncio.gather(*adapter._navigation_tasks)
            replay = await adapter.dispatch(replace(request, params={
                **request.params, "nav_session": result["t4_search"]["session"]}))
            assert replay == result
            assert adapter._navigation_slots._value == 3
        finally:
            release.set()
            await asyncio.gather(*adapter._navigation_tasks)
            adapter.close()

    asyncio.run(run())


def test_incremental_no_results_failure_and_session_mismatch(tmp_path):
    async def run():
        worker = SearchWorker()
        worker.empty = True
        adapter, _ = setup_adapter(tmp_path, worker)
        request = T4Request("search", "source-a", "nav", {
            "wd": "Film", "nav_search": True, "nav_incremental": True})
        try:
            result = await adapter.dispatch(request)
            assert not result["t4_search"]["pending"] and result["total"] == 0
            session = result["t4_search"]["session"]
            with pytest.raises(ValueError, match="会话已过期"):
                await adapter.dispatch(replace(request, params={
                    **request.params, "wd": "Other", "nav_session": session}))
            adapter._navigation_searches.runs.clear()
            worker.broken = {"p1", "p8"}
            with pytest.raises(ValueError, match="失败或超时"):
                await adapter.dispatch(request)
        finally:
            await asyncio.gather(*adapter._navigation_tasks)
            adapter.close()

    asyncio.run(run())


def test_incremental_sessions_and_payloads_are_bounded(tmp_path):
    from sidecar.t4.navigation import NavigationSearches

    sessions = NavigationSearches()
    for i in range(8):
        sessions.get(str(i), "", 0, 2, 25)
    with pytest.raises(ValueError, match="片单过多"):
        sessions.get("ninth", "", 0, 2, 25)
    run = next(iter(sessions.runs.values()))
    run.done = True
    run.touched -= 121
    sessions.get("ninth", "", 0, 2, 25)
    assert len(sessions.runs) == 8
    transformed = []
    rows = [{"vod_id": str(i), "vod_name": "Film", "vod_content": "x" * 20000}
            for i in range(200)]
    run.append(rows, lambda row: transformed.append(row) or row)
    assert run.size <= 768 * 1024
    assert len(run.rows) < 40
    assert len(transformed) == len(run.rows)
    assert run.truncated == 200 - len(run.rows)
    assert len(run.rows) + run.truncated == 200
    for cached in sessions.runs.values():
        cached.done = True
        cached.touched -= 121
    assert sessions.prune() == 8
    assert not sessions.runs
