import os
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from sidecar.worker.client import (
    WorkerConfig,
    WorkerPool,
    WorkerRemoteError,
    WorkerTimeoutError,
    WorkerSession,
)
from sidecar.worker.protocol import WorkerResponse


def test_worker_pool_keeps_one_session_per_source() -> None:
    with WorkerPool(WorkerConfig(request_timeout=2.0)) as pool:
        first = pool.request("source-a", "inspect")
        second = pool.request("source-a", "inspect")
        other = pool.request("source-b", "inspect")

    assert first["pid"] == second["pid"]
    assert first["session_id"] == second["session_id"]
    assert first["pid"] != other["pid"]
    assert first["pid"] != os.getpid()


def test_worker_protocol_round_trips_json_data() -> None:
    with WorkerPool() as pool:
        assert pool.request(
            "source-a",
            "echo",
            {"name": "demo", "values": [1, 2, 3]},
        ) == {"name": "demo", "values": [1, 2, 3]}


def test_worker_timeout_restarts_source_session() -> None:
    # Keep cold startup separate from the deliberately short RPC deadline below.
    with WorkerPool() as pool:
        old = pool.request("source-a", "inspect")
        with pytest.raises(WorkerTimeoutError):
            pool.request(
                "source-a",
                "delay",
                {"seconds": 0.25},
                timeout=0.05,
            )
        new = pool.request("source-a", "inspect")

    assert new["pid"] != old["pid"]
    assert new["session_id"] != old["session_id"]


def test_worker_errors_are_structured_and_recoverable() -> None:
    with WorkerPool() as pool:
        with pytest.raises(WorkerRemoteError) as error:
            pool.request("source-a", "unknown")
        assert error.value.code == "unsupported_method"
        assert pool.request("source-a", "ping") == {"status": "ready"}


@pytest.mark.parametrize("ids,timeout,expected", [
    (["movie-1"], None, 2.0),
    (["atvp_detail:movie-1"], None, 120.0),
    (["atvp_group:fixture"], None, 120.0),
    (["atvp_detail:movie-1"], 0.1, 0.1),
])
def test_only_folder_detail_gets_an_automatic_expansion_budget(ids, timeout, expected):
    class Input:
        def write(self, text):
            self.request = json.loads(text)
        def flush(self):
            pass
    stream = Input()
    class Responses:
        def get(self, *, timeout):
            assert timeout == expected
            return WorkerResponse(stream.request["id"], True, {})
    session = WorkerSession.__new__(WorkerSession)
    session._config = WorkerConfig(request_timeout=2.0)
    session._process = SimpleNamespace(poll=lambda: None, stdin=stream)
    session._lock = threading.Lock()
    session._responses = Responses()
    assert session.request("detail", {"ids": ids}, timeout=timeout) == {}


def test_prefetch_does_not_start_worker_and_remote_failure_preserves_existing_runtime():
    with WorkerPool() as pool:
        assert pool.prefetch("source-a", {"id": "next"}) is None
        assert pool.resource_summary()["workers"] == 0
        old = pool.request("source-a", "inspect")
        # An uninitialized plugin rejects speculative play, but its process stays usable.
        with pytest.raises(WorkerRemoteError):
            pool.prefetch("source-a", {"id": "next"})
        assert pool.request("source-a", "inspect") == old


def test_prefetch_skips_busy_worker_instead_of_queuing():
    with WorkerPool(WorkerConfig(request_timeout=2)) as pool:
        old = pool.request("source-a", "inspect")
        session = pool._sessions["source-a"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            foreground = executor.submit(pool.request, "source-a", "delay", {"seconds": 0.2})
            deadline = time.monotonic() + 2
            while not session._lock.locked() and time.monotonic() < deadline:
                time.sleep(0.001)
            assert session._lock.locked()
            assert pool.prefetch("source-a", {"id": "next"}) is None
            assert foreground.result(timeout=3) == {"status": "complete"}
        assert pool.request("source-a", "inspect") == old


def test_prefetch_uses_worker_budget_and_recovers_a_truly_hung_rpc(tmp_path):
    import base64
    path = tmp_path / "slow.py"
    path.write_text("""
from base.spider import Spider
import time
class Spider(Spider):
    def init(self, extend=""): pass
    def getName(self): return "slow"
    def playerContent(self, flag, id, vipFlags):
        time.sleep(1)
        return {"url": "https://cdn.example/next.mp4"}
""", encoding="utf-8")
    extend = base64.b64encode(json.dumps({"loader": str(path)}).encode()).decode()
    with WorkerPool(WorkerConfig(request_timeout=10)) as pool:
        pool.request("source-a", "init", {"extend": extend})
        old = pool.request("source-a", "inspect")
        pool._sessions["source-a"]._config = WorkerConfig(request_timeout=0.05)
        with pytest.raises(WorkerTimeoutError):
            pool.prefetch("source-a", {"id": "next"})
        assert not pool.has_session("source-a")
        assert pool.request("source-a", "inspect")["session_id"] != old["session_id"]
