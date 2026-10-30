from concurrent.futures import ThreadPoolExecutor
import time

import httpx

from sidecar.diagnostics import TraceStore, trace_scope
from sidecar.t4.adapter import AtvpWorkerInvoker
from sidecar.worker.client import WorkerConfig, WorkerPool
from tests.t4.test_adapter import descriptor
from dataclasses import replace


SOURCE = '''
from base.spider import Spider as Base
class Spider(Base):
    def init(self, extend): self.items = {}
    def getName(self): return "idle fixture"
    def homeContent(self, filter):
        self.items["folder"] = "remembered"
        return {"list": [{"vod_id": "folder", "vod_tag": "folder"}]}
    def categoryContent(self, tid, pg, filter, extend):
        assert self.items[tid] == "remembered"
        self.items["file"] = "remembered"
        return {"list": [{"vod_id": "file", "vod_tag": "file"}]}
    def searchContent(self, key, quick, pg="1"):
        self.items[key] = "remembered"
        return {"list": [{"vod_id": key, "vod_name": key}]}
    def detailContent(self, ids):
        return {"list": [{"vod_id": i, "vod_name": self.items[i]} for i in ids]}
    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "url": self.getProxyUrl()}
    def localProxy(self, params):
        return [200, "video/mp2t", b"retained"]
'''


def plugin_for(tmp_path):
    path = tmp_path / "idle.py"
    path.write_text(SOURCE, encoding="utf-8")
    return replace(descriptor(), loader=str(path), source=str(path), raw=True)


def test_idle_browse_workers_retire_and_restore_exact_selected_search(tmp_path):
    pool = WorkerPool(WorkerConfig(request_timeout=10))
    invoker = AtvpWorkerInvoker(pool)
    plugin = plugin_for(tmp_path)
    store = TraceStore(console=False)
    try:
        with trace_scope(store, trace_id="idle"):
            for key in ("old", "latest"):
                invoker.call("demo", plugin, "search", {"key": key, "quick": False})
            original = pool.request("demo", "inspect")
            assert pool.retire_idle(idle_seconds=0) == 1
            assert not pool.has_session("demo")
            # A newer search after respawn must not hide the old card's breadcrumb.
            invoker.call("demo", plugin, "search", {"key": "new-generation"})
            result = invoker.call("demo", plugin, "detail", {"ids": ["old", "latest"]})
            assert [row["vod_name"] for row in result["list"]] == ["remembered"] * 2
            assert pool.request("demo", "inspect") != original
            assert pool.retire_idle(idle_seconds=0) == 0
            assert pool.resource_summary()["retained_workers"] == 1
            assert any(e["stage"] == "worker.context_restored" for e in store.events())
    finally:
        invoker.close()


def test_browse_store_keeps_home_and_nested_category_ancestry():
    from sidecar.worker.browse import BrowseContextStore
    store = BrowseContextStore()
    try:
        store.remember("demo", "home", {"filter": True}, {"list": []}, 1)
        store.remember("demo", "category", {"tid": "root"},
                       {"list": [{"vod_id": "folder", "vod_tag": "folder"}]}, 1)
        store.remember("demo", "category", {"tid": "folder"}, {"list": [{"vod_id": "movie"}]}, 1)
        assert store.for_detail("demo", ["movie"], 2) == [
            ["home", {"filter": True}],
            ["category", {"tid": "root"}],
            ["category", {"tid": "folder"}],
        ]
        assert store.for_detail("demo", ["movie"], 1) == []
        store.restored("demo", ["movie"], 2)
        assert store.for_detail("demo", ["movie"], 2) == []
        store.remove("demo")
        assert store.for_detail("demo", ["movie"], 3) == []
        store.remember("demo", "home", {}, {"class": [{"type_id": "1"}]}, 1)
        store.remember("demo", "search", {"key": "same-id"}, {"list": [{"vod_id": "1"}]}, 1)
        assert store.for_detail("demo", ["1"], 2, kind="category") == [["home", {}]]
        assert store.for_detail("demo", ["1"], 2)[-1] == ["search", {"key": "same-id"}]
    finally:
        store.close()


def test_old_folder_and_its_detail_work_after_separate_retirements(tmp_path):
    pool = WorkerPool(WorkerConfig(request_timeout=10))
    invoker = AtvpWorkerInvoker(pool)
    plugin = plugin_for(tmp_path)
    try:
        invoker.call("demo", plugin, "home", {})
        assert pool.retire_idle(idle_seconds=0) == 1
        assert invoker.call("demo", plugin, "category", {"tid": "folder"})["list"][0]["vod_id"] == "file"
        assert pool.retire_idle(idle_seconds=0) == 1
        assert invoker.call("demo", plugin, "detail", {"ids": ["file"]})["list"][0]["vod_name"] == "remembered"
    finally:
        invoker.close()


def test_idle_retirement_never_closes_proxy_or_affinity_lease(tmp_path):
    pool = WorkerPool(WorkerConfig(request_timeout=10))
    invoker = AtvpWorkerInvoker(pool)
    plugin = plugin_for(tmp_path)
    try:
        invoker.call("browse", plugin, "search", {"key": "first"})
        with pool.lease("browse"):
            assert pool.retire_idle(idle_seconds=0) == 0
        assert pool.retire_idle(idle_seconds=0) == 1
        play = invoker.call("play", plugin, "play", {"id": "any"})
        assert pool.retire_idle(idle_seconds=0) == 0
        assert httpx.get(pool.proxy_endpoint("play"), trust_env=False).content == b"retained"
    finally:
        invoker.close()


def test_retirement_skips_busy_and_recent_and_limits_batch():
    with WorkerPool(WorkerConfig(request_timeout=10)) as pool:
        for index in range(3):
            pool.request(str(index), "ping")
        assert pool.retire_idle() == 0
        assert pool.retire_idle(idle_seconds=0, batch_size=2) == 2
        assert pool.resource_summary()["workers"] == 1
        with ThreadPoolExecutor(max_workers=1) as threads:
            future = threads.submit(pool.request, "2", "delay", {"seconds": .2})
            until = time.monotonic() + 3
            while not pool.resource_summary()["busy_workers"] and time.monotonic() < until:
                time.sleep(.001)
            assert pool.retire_idle(idle_seconds=0) == 0
            assert future.result(timeout=3) == {"status": "complete"}
        assert pool.retire_idle(idle_seconds=0) == 1


def test_browse_store_can_reopen_after_gate_batch_close(tmp_path):
    pool = WorkerPool(WorkerConfig(request_timeout=10))
    invoker = AtvpWorkerInvoker(pool)
    plugin = plugin_for(tmp_path)
    try:
        for _ in range(2):
            invoker.call("demo", plugin, "search", {"key": "again"})
            assert pool.retire_idle(idle_seconds=0) == 1
            assert invoker.call("demo", plugin, "detail", {"ids": ["again"]})["list"]
            invoker.close()
    finally:
        invoker.close()
