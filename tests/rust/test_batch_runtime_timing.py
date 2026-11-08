import asyncio
import gc
import json
import sys
import threading

import httpcore
import pytest

from scripts.diagnose_rust_gateway import BatchRuntimeTiming


class Pool:
    def _assign_requests_to_connections(self, value=None):
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.mark.parametrize("failure", [None, ValueError, asyncio.CancelledError])
def test_batch_probe_preserves_returns_errors_gc_and_restores(failure):
    pool, unrelated = Pool(), Pool()
    callbacks = list(gc.callbacks)
    original = pool._assign_requests_to_connections
    timing = BatchRuntimeTiming([pool])
    secret = object()
    try:
        with timing:
            assert pool._assign_requests_to_connections(secret) is secret
            assert unrelated._assign_requests_to_connections(secret) is secret
            gc.collect(0)
            if failure:
                pool._assign_requests_to_connections(failure("private-media-url"))
    except BaseException as exc:
        assert failure is not None and type(exc) is failure
    assert pool._assign_requests_to_connections == original
    assert "_assign_requests_to_connections" not in pool.__dict__
    assert "_t4_batch_timing_owner" not in pool.__dict__
    assert gc.callbacks == callbacks
    result = timing.result
    assert result["valid"] and result["restored"]
    assert result["started_ns"] < result["finished_ns"]
    assert result["main_cpu_ns"] >= 0
    assert result["gc"]["calls"] >= 1 and result["gc"]["by_generation"][0] >= 1
    assert result["gc"]["max_wall_ns"] > 0
    stats = result["pools"][0]
    assert stats["calls"] == 1 + bool(failure)
    assert stats["errors"] == ({failure.__name__: 1} if failure else {})
    assert stats["wall_ns"] >= stats["max_wall_ns"] > 0 and stats["cpu_ns"] >= 0
    assert len(stats["source_sha256"]) == 64
    assert "private-media-url" not in json.dumps(result)
    if sys.platform == "linux":
        assert result["main_thread_sched"]["available"]
    else:
        assert not result["main_thread_sched"]["available"]


def test_batch_probe_uses_real_httpcore_owner_and_rejects_nested_or_duplicate_pools():
    pool = httpcore.AsyncConnectionPool()
    with pytest.raises(ValueError):
        BatchRuntimeTiming([pool, pool])
    with BatchRuntimeTiming([pool]) as timing:
        assert pool._assign_requests_to_connections() == []
        with pytest.raises(RuntimeError):
            with BatchRuntimeTiming([pool]):
                pass
    assert timing.result["pools"][0]["calls"] == 1
    with BatchRuntimeTiming([pool]) as later:
        pool._assign_requests_to_connections()
    assert later.result["pools"][0]["calls"] == 1
    assert later.result["pools"][0]["source_sha256"] == timing.result["pools"][0]["source_sha256"]


def test_batch_probe_partial_install_failure_restores_earlier_pool():
    pool = Pool()
    callbacks = list(gc.callbacks)
    with pytest.raises(AttributeError):
        with BatchRuntimeTiming([pool, object()]):
            pass
    assert not pool.__dict__ and gc.callbacks == callbacks


def test_batch_probe_bounds_gc_records_and_excludes_foreign_thread_cpu():
    pool = Pool()
    with BatchRuntimeTiming([pool]) as timing:
        thread = threading.Thread(target=pool._assign_requests_to_connections)
        thread.start()
        thread.join()
        for _ in range(130):
            gc.collect(0)
    assert timing.result["foreign_pool_calls"] == 1
    assert timing.result["pools"][0]["calls"] == 0
    assert timing.result["gc"]["calls"] >= 130
    assert timing.result["gc"]["truncated"]
    assert len(timing.result["gc"]["events"]) == 128


def test_batch_probe_rejects_incomplete_gc_evidence():
    with BatchRuntimeTiming([Pool()]) as timing:
        timing._gc("stop", {"generation": 2})
    assert not timing.result["valid"] and timing.result["gc"]["errors"] == 1
