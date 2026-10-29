import os
import json
import threading
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
    with WorkerPool(WorkerConfig(request_timeout=1.0)) as pool:
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
