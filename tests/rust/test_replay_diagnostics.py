import asyncio
import gc
import hashlib
import json

import httpx
import pytest

from scripts import diagnose_rust_replay as replay
from tests.rust.relay_support import MIB


def payload():
    return (b"\x47" + b"\x00" * 187) * (MIB // 188 + 1)


def test_conditions_keep_completion_warmup_and_chunk_factors_separate():
    assert len(replay.conditions()) == 6
    assert {(eof, warm) for block, eof, warm in replay.conditions() if block == "16k"} == {
        (False, False), (False, True), (True, False), (True, True)}
    assert all(eof and warm for block, eof, warm in replay.conditions() if block != "16k")


def test_factorial_plan_covers_full_rank_cells_and_balances_each_mode_position():
    from scripts.analyze_rust_replay_math import matrix_rank
    from scripts.benchmark_rust_selected_media import batch_modes

    expected = {(n, kib) for n in (1, 4, 8, 16) for kib in (64, 256, 1024)}
    assert set(replay.factorial_conditions()) == expected
    assert matrix_rank([[1, n, n * n, n * kib] for n, kib in expected]) == 4
    positions = {cell: [] for cell in expected}
    for repeat in range(3):
        order = replay.factorial_order(repeat)
        assert len(order) == 12 and {(n, kib) for _, n, kib in order} == expected
        for position, (index, n, kib) in enumerate(order):
            positions[n, kib].append(position)
            mode_round = (repeat + index) % 3 + 3 * (index % 2)
            assert set(batch_modes(mode_round, aa=True)) == {"python", "rust", "python_aa"}
    assert all(len(set(p)) == 3 for p in positions.values())
    for index in range(12):
        orders = [batch_modes((r + index) % 3 + 3 * (index % 2), aa=True) for r in range(3)]
        assert all(len({order[p] for order in orders}) == 3 for p in range(3))


def test_socket_lookup_plan_requires_full_paired_conditions_and_keeps_failures():
    rows = []
    for mode in ("python", "rust"):
        for n, kib in ((1, 64), (16, 64), (1, 1024), (16, 1024)):
            for repeat in range(4):
                for fast in (False, True):
                    rows.append({"mode": mode, "concurrency": n, "per_stream_kib": kib,
                        "repeat": repeat, "gateway_socket_fastpath": fast,
                        "complete_range": True, "warmup": True, "profile": False,
                        "runtime_timing": False, "passed": True, "connection_evidence_passed": True,
                        "bytes": n * kib * 1024, "gateway_counts": {"socket_fastpath_calls": 10 if fast else 0},
                        "ttfb_p95_ms": 10 + repeat + (20 if fast and repeat == 3 else 0),
                        "mib_per_second": 100 if not fast else 100 * (.90 if n == 16 and repeat == 2 else 1.01),
                        "gateway_variant_position": int(fast != bool((repeat + (n + kib) % 4) % 2))})
    assert replay.socket_study_complete(rows)
    effects = replay.socket_study_effect(rows)
    assert len(effects) == 8
    high = next(v for v in effects if v["relay_mode"] == "rust" and v["concurrency"] == 16
                and v["per_stream_kib"] == 64)
    assert high["failed_repeats"] == [2, 3]
    with pytest.raises(ValueError):
        replay.socket_study_effect(rows[:-1])


@pytest.mark.parametrize("fault", [None, "missing", "duplicate", "wrong_bytes", "probe", "no_eof"])
def test_factorial_gate_does_not_accept_partial_or_mixed_workload(fault):
    rows = [{"concurrency": n, "per_stream_kib": kib, "repeat": r, "mode": mode,
             "bytes": n * kib * 1024, "passed": True, "connection_evidence_passed": True,
             "profile": False, "runtime_timing": False, "complete_range": True, "warmup": True}
            for n, kib in replay.factorial_conditions() for r in range(3)
            for mode in ("python", "rust", "python_aa")]
    if fault == "missing":
        rows.pop()
    elif fault == "duplicate":
        rows[-1] = dict(rows[0])
    elif fault == "wrong_bytes":
        rows[0]["bytes"] += 1
    elif fault == "probe":
        rows[0]["profile"] = True
    elif fault == "no_eof":
        rows[0]["complete_range"] = False
    assert replay.factorial_complete(rows) == (fault is None)


@pytest.mark.parametrize("kib", [64, 256, 1024])
def test_factorial_prefix_reference_keeps_same_captured_generation(kib):
    async def check():
        body = payload()[:MIB]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=replay.replay_origin(body, "video/mp2t")),
                                     base_url="http://test") as client:
            result = await replay.prefix_read(client, "http://test/bytes", {}, kib * 1024, complete_range=True)
        assert result["passed"] and result["response_exhausted"]
        assert result["range_metadata"] == [0, kib * 1024 - 1, MIB]
        assert result["bytes"] == kib * 1024
        assert result["sha256"] == hashlib.sha256(body[:kib * 1024]).hexdigest()
    asyncio.run(check())


@pytest.mark.parametrize("valid", [True, False, None])
def test_replay_cpu_evidence_rejects_invalid_or_missing_profiles(valid):
    row = {"passed": True, "connection_evidence_passed": True, "profile": True}
    if valid is not None:
        row["consumer_cpu_profile"] = {"valid": valid}
    assert replay.row_evidence_passed(row) == (valid is True)
    row["profile"] = False
    assert replay.row_evidence_passed(row)
    row["connection_evidence_passed"] = False
    assert not replay.row_evidence_passed(row)


@pytest.mark.parametrize("problem", [None, "missing", "invalid", "cleanup", "sched", "zero_calls",
                                    "exception", "foreign", "gc_overflow", "window", "order"])
def test_replay_runtime_evidence_requires_both_owners_and_matching_windows(problem):
    def timing(start, end):
        return {"valid": True, "restored": True, "started_ns": start, "finished_ns": end,
                "main_thread_sched": {"available": True}, "foreign_pool_calls": 0,
                "gc": {"truncated": False}, "pools": [{"calls": 32, "errors": {}}]}
    row = {"passed": True, "connection_evidence_passed": True, "profile": False,
           "runtime_timing": True, "concurrency": 16,
           "gateway_runtime_timing": timing(10, 100), "consumer_runtime_timing": timing(20, 90)}
    observed = row["consumer_runtime_timing"]
    if problem == "missing":
        del row["consumer_runtime_timing"]
    elif problem == "invalid":
        observed["valid"] = False
    elif problem == "cleanup":
        observed["restored"] = False
    elif problem == "sched":
        observed["main_thread_sched"]["available"] = False
    elif problem == "zero_calls":
        observed["pools"][0]["calls"] = 0
    elif problem == "exception":
        observed["pools"][0]["errors"] = {"RuntimeError": 1}
    elif problem == "foreign":
        observed["foreign_pool_calls"] = 1
    elif problem == "gc_overflow":
        observed["gc"]["truncated"] = True
    elif problem == "window":
        observed["started_ns"] = 9
    elif problem == "order":
        observed["finished_ns"] = 19
    assert replay.row_evidence_passed(row) == (problem is None)


def test_replay_gateway_batch_control_is_exclusive_and_lifespan_restores(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"real": {"url": "http://127.0.0.1:1/bytes", "headers": {}}}))
    callbacks = list(gc.callbacks)

    async def check():
        app = replay.replay_gateway(manifest, "16k")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.post("/__replay_timing/stop")).status_code == 409
                assert (await client.post("/__replay_timing/start")).status_code == 200
                assert (await client.post("/__replay_timing/start")).status_code == 409
                stopped = (await client.post("/__replay_timing/stop")).json()
                assert stopped["valid"] and stopped["restored"]
                assert sum(p["calls"] for p in stopped["pools"]) == 0
                assert gc.callbacks == callbacks
                assert (await client.post("/__replay_timing/start")).status_code == 200
        assert gc.callbacks == callbacks
    asyncio.run(check())


@pytest.mark.parametrize("method,range_value,status", [
    ("GET", "bytes=0-1023", 206), ("HEAD", "bytes=0-1023", 206),
    ("GET", "bytes=2-1023", 416), ("GET", "bytes=0-2000000", 416), ("GET", "", 416),
])
def test_replay_origin_is_exact_and_bounded(method, range_value, status):
    async def check():
        body = payload()[:MIB]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=replay.replay_origin(body, "video/mp2t")),
                                     base_url="http://test") as client:
            response = await client.request(method, "/bytes", headers={"Range": range_value})
        assert response.status_code == status
        if status == 206:
            assert response.headers["content-range"] == f"bytes 0-1023/{MIB}"
            assert response.content == (body[:1024] if method == "GET" else b"")
    asyncio.run(check())


@pytest.mark.parametrize("problem", [None, "status", "range", "short", "overflow", "html", "throttle"])
def test_capture_keeps_only_verified_bounded_bytes(monkeypatch, tmp_path, problem):
    body = payload()[:MIB]
    headers = {"Content-Type": "video/mp2t", "Content-Range": f"bytes 0-{MIB - 1}/{MIB}",
               "Content-Length": str(MIB)}
    if problem == "range":
        headers["Content-Range"] = "bytes 1-10/20"
    if problem == "short":
        body = body[:500]
    if problem == "overflow":
        body += b"extra"
    if problem == "html":
        body = b"<html>" + b" " * (MIB - 6)

    def handler(request):
        assert request.headers["range"] == f"bytes=0-{MIB-1}"
        return httpx.Response(429 if problem == "throttle" else 200 if problem == "status" else 206,
                              headers=headers, stream=httpx.ByteStream(body))

    original = httpx.AsyncClient
    monkeypatch.setattr(replay.httpx, "AsyncClient", lambda **kw: original(
        **kw, transport=httpx.MockTransport(handler)))
    snapshot = tmp_path / "sample.bin"
    result = asyncio.run(replay.capture({"url": "http://private.invalid/media?secret=x", "headers": {}}, snapshot))
    assert (result["status"] == "pass") == (problem is None)
    assert snapshot.exists() == (problem is None)
    assert "private" not in json.dumps(result) and "secret" not in json.dumps(result)
    if problem is None:
        assert snapshot.read_bytes() == body
        assert result["sha256"] == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("block,expected_chunks", [("16k", 64), ("64k", 16), ("native", 1)])
def test_gateway_chunk_control_invokes_actual_segment_path(tmp_path, monkeypatch, block, expected_chunks):
    from sidecar.playback import PlaybackBridge

    body = payload()[:MIB]
    original_init = PlaybackBridge.__init__

    def init(self, *args, **kwargs):
        def handler(request):
            return httpx.Response(206, headers={"Content-Length": str(MIB), "Content-Type": "video/mp2t",
                "Content-Range": f"bytes 0-{MIB-1}/{MIB}"}, stream=httpx.ByteStream(body))
        original_init(self, *args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(PlaybackBridge, "__init__", init)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"real": {"url": "http://fixture.invalid/bytes", "headers": {}}}))

    async def check():
        app = replay.replay_gateway(manifest, block)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/t4/media/real", headers={"Range": f"bytes=0-{MIB-1}"})
                assert response.status_code == 206 and response.content == body
                counters = (await client.get("/__replay_metrics")).json()
                assert counters == {"iterator_calls": 1, "yielded_chunks": expected_chunks,
                                    "yielded_bytes": MIB, "socket_fastpath_calls": 0}
    asyncio.run(check())
