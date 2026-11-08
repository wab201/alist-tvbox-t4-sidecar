import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

import pytest
import httpx
import uvicorn

from scripts.benchmark_rust_selected_media import ProcessConsumer, comparison, pool_effect_comparison
from scripts import benchmark_rust_selected_media as benchmark
from tests.rust.relay_support import MIB, binary_path, experiment, pattern


def test_aa_order_balances_positions_and_predecessors_without_altering_probe_rows():
    from collections import Counter

    modes = {"python", "rust", "python_aa"}
    orders = [benchmark.batch_modes(repeat, aa=True) for repeat in range(6)]
    for block in (orders[:3], orders[3:]):
        for position in range(3):
            assert {order[position] for order in block} == modes
    predecessors = Counter(pair for order in orders for pair in zip(order, order[1:]))
    assert len(predecessors) == 6 and set(predecessors.values()) == {2}
    for repeat in range(12):
        assert benchmark.batch_modes(repeat, aa=True) == orders[repeat % 6]
        expected = ("python", "rust") if repeat % 2 == 0 else ("rust", "python")
        assert benchmark.batch_modes(repeat, aa=True, probe=True) == expected
        assert benchmark.batch_modes(repeat) == expected


@pytest.mark.parametrize("field", [None, "sha256", "bytes", "status", "mime", "range_metadata"])
def test_warmup_byte_gate_checks_exact_reference(field):
    reference = {"passed": True, "sha256": "expected", "bytes": 16, "status": 206,
                 "mime": "video/mp4", "range_metadata": [0, 15, 100]}
    sample = dict(reference)
    if field:
        sample[field] = None
    assert benchmark.byte_contract_matches([sample], reference) == (field is None)
    assert not benchmark.byte_contract_matches([], reference)


@pytest.mark.parametrize("reserved", [False, True])
def test_aa_rows_do_not_pollute_original_performance_gate(tmp_path, reserved):
    runner = object.__new__(benchmark.PerformanceRunner)
    runner.performance = SimpleNamespace(output=tmp_path / "aa.json")
    runner.gateway_probe = None
    result = {key: [] for key in ("rows", "profile_rows", "pool_rows", "pool_profile_rows",
                                 "aa_rows", "pool_aa_rows")}
    row = {"mode": "python_aa", "repeat": 0, "concurrency": 16, "probe": False,
           "pool_policy": "reserved_http1" if reserved else "original", "passed": True,
           "mib_per_second": 10, "ttfb_p95_ms": 1}
    asyncio.run(runner._record_batch(result, row, None))
    assert result["pool_aa_rows" if reserved else "aa_rows"] == [row]
    assert result["rows"] == result["pool_rows"] == []


@pytest.mark.parametrize("kind", ["asyncio", "uvicorn"])
def test_gateway_loop_uses_requested_factory_and_reports_actual_type(monkeypatch, kind):
    factories = []

    def get_factory(self):
        def create():
            factories.append(True)
            return asyncio.new_event_loop()
        return create

    async def fake_audit(args):
        loop = asyncio.get_running_loop()
        assert isinstance(loop, asyncio.AbstractEventLoop) and loop.is_running()
        return 17

    monkeypatch.setattr(uvicorn.Config, "get_loop_factory", get_factory)
    monkeypatch.setattr(benchmark, "audit", fake_audit)
    assert benchmark.run_audit(SimpleNamespace(gateway_loop=kind)) == 17
    assert len(factories) == (kind == "uvicorn")


def test_safe_failure_does_not_capture_messages_source_or_locals():
    try:
        try:
            raise ValueError("secret-cookie-and-address")
        except ValueError as exc:
            raise httpx.RemoteProtocolError("private-url") from exc
    except Exception as exc:
        failure = benchmark.safe_failure(exc, "gateway_timeline_collect")
    assert [c["type"] for c in failure["error_chain"]] == ["RemoteProtocolError", "ValueError"]
    encoded = json.dumps(failure)
    assert "secret-cookie" not in encoded and "private-url" not in encoded
    for cause in failure["error_chain"]:
        assert cause["frames"]
        for frame in cause["frames"]:
            assert set(frame) == {"file", "function", "line"}
            assert frame["file"] == "test_selected_media_performance.py"


@pytest.mark.parametrize("reserved", [False, True])
@pytest.mark.parametrize("problem", ["error", "incomplete", "none", "off"])
def test_diagnostic_failure_preserves_media_batch(monkeypatch, tmp_path, reserved, problem):
    calls = []

    async def collect(*args):
        calls.append(True)
        if problem == "error":
            raise httpx.RemoteProtocolError("private-media-url")
        return [{"complete": problem == "none"}]

    runner = object.__new__(benchmark.PerformanceRunner)
    runner.gateway_probe = SimpleNamespace(collect=collect)
    runner.performance = SimpleNamespace(output=tmp_path / "assessment.json")
    result = {name: [] for name in ("rows", "profile_rows", "pool_rows", "pool_profile_rows")}
    row = {"mode": "python", "repeat": 0, "concurrency": 1, "passed": True,
           "probe": problem != "off", "pool_policy": "reserved_http1" if reserved else "original",
           "mib_per_second": 100, "ttfb_p95_ms": 10, "bytes": 1024, "samples": [{"passed": True}]}
    asyncio.run(runner._record_batch(result, row, None))
    key = ("pool_" if reserved else "") + ("rows" if problem == "off" else "profile_rows")
    assert result[key] == [row] and sum(len(v) for v in result.values()) == 1
    saved = json.loads(runner.performance.output.with_suffix(".rows.jsonl").read_text())
    assert saved == row and saved["passed"] and saved["bytes"] == 1024
    assert len(calls) == (problem != "off")
    assert "private-media-url" not in json.dumps(saved)
    if problem == "error":
        assert saved["diagnostic_failure"]["failure_stage"] == "gateway_timeline_collect"
    if problem != "off":
        assert saved["timeline_complete"] == (problem == "none")


def test_fixed_media_performance_requires_all_pairs_and_verified_bytes():
    rows = [{"concurrency": 16, "repeat": repeat, "mode": mode, "passed": True,
             "ttfb_p95_ms": 20 + (repeat if mode == "rust" else 0),
             "mib_per_second": 100}
            for repeat in range(3) for mode in ("python", "rust")]
    summary = comparison(rows, 3)
    assert not any(r["complete"] for r in summary[:3])
    assert summary[-1]["complete"] and summary[-1]["paired_ttfb_changes_ms"] == [0, 1, 2]
    assert summary[-1]["paired_performance_passed"]
    rows[-1]["passed"] = False
    assert not comparison(rows, 3)[-1]["complete"]


@pytest.mark.parametrize("problem", [None, "late_install", "no_receive", "missing_body"])
def test_body_timing_requires_live_receive_and_byte_coverage(tmp_path, problem):
    async def collect(*args):
        return [{"complete": True}]

    runner = object.__new__(benchmark.PerformanceRunner)
    runner.gateway_probe = SimpleNamespace(collect=collect)
    runner.performance = SimpleNamespace(output=tmp_path / "coverage.json")
    result = {name: [] for name in ("rows", "profile_rows", "pool_rows", "pool_profile_rows")}
    row = {"mode": "python", "repeat": 0, "concurrency": 1, "passed": True,
           "probe": True, "pool_policy": "original", "mib_per_second": 100,
           "ttfb_p95_ms": 10, "bytes": 1024, "samples": [{"passed": True}],
           "gateway_body_timing": {"receive_installed_before_connect": problem != "late_install", "owners": {
               "anyio_data_received": {"calls": 0 if problem == "no_receive" else 1},
               "asgi_body_send": {"bytes": 1024},
               "transport_body_write": {"bytes": 0 if problem == "missing_body" else 1024}}}}
    asyncio.run(runner._record_batch(result, row, None))
    assert row["body_timing_complete"] == (problem is None)
    assert row["passed"] and row["timeline_complete"]
    assert ("diagnostic_failure" in row) == (problem is not None)


def paired_rows():
    return [{"concurrency": 16, "repeat": repeat, "mode": mode, "passed": True,
             "ttfb_p95_ms": latency, "mib_per_second": 100}
            for repeat, latencies in enumerate(((1000, 1000), (100, 120), (1000, 500)))
            for mode, latency in zip(("python", "rust"), latencies)]


def test_paired_gate_does_not_hide_regression_behind_medians():
    result = comparison(paired_rows(), 3)[-1]
    assert result["complete"] and result["median_change_ms"] == 0
    assert result["failed_repeats"] == [1]
    assert not result["paired_performance_passed"]


def test_paired_gate_uses_existing_latency_and_throughput_thresholds():
    rows = paired_rows()
    for row in rows:
        row.update(ttfb_p95_ms=10 if row["mode"] == "python" else 12,
                   mib_per_second=100 if row["mode"] == "python" else 95)
    assert comparison(rows, 3)[-1]["paired_performance_passed"]
    rows[-1]["mib_per_second"] = 94.99
    result = comparison(rows, 3)[-1]
    assert result["complete"] and result["failed_repeats"] == [2]
    assert not result["paired_performance_passed"]


@pytest.mark.parametrize("problem", ["duplicate", "wrong_repeat", "missing", "nan", "zero"])
def test_paired_gate_rejects_invalid_evidence(problem):
    rows = paired_rows()
    if problem == "duplicate":
        rows.append(dict(rows[-1]))
    elif problem == "wrong_repeat":
        rows[-1]["repeat"] = 4
    elif problem == "missing":
        rows.pop()
    else:
        rows[-1]["ttfb_p95_ms"] = float("nan") if problem == "nan" else 0
    result = comparison(rows, 3)[-1]
    assert not result["complete"] and not result["paired_performance_passed"]


def test_pool_effect_keeps_each_proxy_and_each_round_separate():
    control = [{"concurrency": concurrency, "repeat": repeat, "mode": mode,
                "passed": True, "ttfb_p95_ms": 100, "mib_per_second": 100}
               for concurrency in (1, 4, 8, 16) for repeat in range(3)
               for mode in ("python", "rust")]
    candidate = [{**r, "ttfb_p95_ms": 50, "mib_per_second": 200} for r in control]
    candidate[-1]["mib_per_second"] = 90
    result = pool_effect_comparison(control, candidate, 3)
    assert [r["relay_mode"] for r in result] == ["python", "rust"]
    assert all(r["paired_performance_passed"] for r in result[0]["comparisons"])
    assert result[1]["comparisons"][-1]["failed_repeats"] == [2]
    assert not result[1]["comparisons"][-1]["paired_performance_passed"]
    candidate.pop()
    assert not pool_effect_comparison(control, candidate, 3)[1]["comparisons"][-1]["complete"]


@pytest.mark.parametrize("regression", [False, True])
@pytest.mark.parametrize("only_concurrency", [None, 16])
def test_audit_exit_distinguishes_byte_contract_from_performance(monkeypatch, tmp_path, regression, only_concurrency):
    rows = [{"concurrency": concurrency, "repeat": repeat, "mode": mode,
             "passed": True, "bytes": 1024, "ttfb_p95_ms": 10, "mib_per_second": 100}
            for concurrency in (1, 4, 8, 16) for repeat in range(3) for mode in ("python", "rust")]
    if only_concurrency:
        rows = [r for r in rows if r["concurrency"] == only_concurrency]
    if regression:
        rows[-1]["ttfb_p95_ms"] = 30

    async def fake_acquisition(args, *, runner_factory):
        runner = runner_factory()
        runner.records.append({"status": "pass", "rows": rows, "comparisons": comparison(rows, 3)})
        return {"rounds": [{"plugins": []}]}

    binary = tmp_path / "identity-only"
    binary.write_bytes(b"test")
    monkeypatch.setattr(benchmark, "binary_path", lambda: binary)
    monkeypatch.setattr(benchmark, "runtime_identity", lambda: {})
    monkeypatch.setattr(benchmark, "PerformanceRunner", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(benchmark, "run", fake_acquisition)
    output = tmp_path / "assessment.json"
    args = SimpleNamespace(consumer_process=True, output=output, select=["ordinary:fixture"],
                           only_concurrency=only_concurrency)
    code = asyncio.run(benchmark.audit(args))
    report = json.loads(output.read_text())
    assert report["complete"] and report["byte_contracts_passed"]
    assert report["only_concurrency"] == only_concurrency
    assert report["paired_performance_passed"] == (not regression and only_concurrency is None)
    assert code == (2 if regression or only_concurrency else 0)
    assert not report["production_ready"]


@pytest.mark.skipif(not binary_path().is_file(), reason="release relay required")
@pytest.mark.parametrize("loop", ["asyncio", "uvicorn"])
@pytest.mark.parametrize("pool_layout", ["shared", "per_stream", "elide_scan"])
def test_persistent_consumer_is_separate_and_checks_both_relays(loop, pool_layout):
    async def check(instances):
        consumer = ProcessConsumer(loop=loop, pool_layout=pool_layout)
        try:
            await consumer.start()
            assert consumer.identity["pid"] != os.getpid()
            assert consumer.identity["event_loop"]
            assert consumer.identity["pool_layout"] == pool_layout
            if pool_layout == "elide_scan":
                assert consumer.identity["idle_scan_experiment"]["expression_replacements"] == 1
            if os.name != "nt" and loop == "uvicorn":
                assert consumer.identity["event_loop"] == "uvloop.Loop"
            for mode in ("python", "rust"):
                row = await consumer.batch(instances[mode].url + "/t4/media/file", 4, 8192, True,
                                           complete_range=True)
                assert row["seconds"] > 0 and row["consumer_cpu_seconds"] >= 0
                clock = row["consumer_clock_window"]
                assert clock["schema"] == "nested-batch-cpu-clocks-v1"
                assert clock["wall_ns"] == clock["finished_ns"] - clock["started_ns"]
                assert clock["wall_ns"] >= row["seconds"] * 1e9
                assert clock["main_cpu_ns"] >= 0 and clock["process_cpu_ns"] >= 0
                assert clock["main_clock_resolution_ns"] > 0
                if os.name != "nt":
                    assert clock["valid"]
                assert row["consumer_pool_after"]["target_idle"] == 4
                assert row["consumer_pool_after"]["queued_or_active"] == 0
                identities = row["consumer_connections"]
                assert identities["new_tcp_connections"] == 4
                assert identities["unique_connections"] == 4
                assert identities["unobserved_responses"] == 0
                assert identities["reused_responses"] == 0
                assert len(row["samples"]) == 4
                assert all(s["passed"] and s["bytes"] == 8192
                           and s["sha256"] == hashlib.sha256(pattern(0, 8192)).hexdigest()
                           for s in row["samples"])
                reused = await consumer.batch(instances[mode].url + "/t4/media/file", 4, 8192, True,
                                              complete_range=True)
                assert reused["consumer_pool_before"]["target_idle"] == 4
                assert reused["consumer_pool_after"]["target_idle"] == 4
                assert reused["consumer_connections"]["new_tcp_connections"] == 0
                assert reused["consumer_connections"]["reused_responses"] == 4
                assert set(reused["consumer_connections"]["response_connection_ids"]) <= set(
                    identities["response_connection_ids"])
                observed = await consumer.batch(instances[mode].url + "/t4/media/file", 4, 8192, True,
                                                complete_range=True, runtime_timing=True)
                timing = observed["consumer_runtime_timing"]
                assert timing["valid"] and timing["restored"]
                assert sum(p["calls"] for p in timing["pools"]) >= 8
                assert len(timing["pools"]) == (16 if pool_layout == "per_stream" else 1)
                assert all(s["passed"] for s in observed["samples"])
                with pytest.raises(RuntimeError, match="ValueError"):
                    await consumer.batch(instances[mode].url, 1, 8192,
                                         profile_cpu=True, runtime_timing=True)
                profiled = await consumer.batch(instances[mode].url + "/t4/media/file", 1, MIB, True,
                                               complete_range=True, profile_cpu=True)
                assert profiled["samples"][0]["passed"]
                profile = profiled["consumer_cpu_profile"]
                if os.name != "nt":
                    assert profile["valid"], profile
                # Windows short CPU-clock samples may be rejected. Preserve that
                # rejection over IPC; only Linux is the performance acceptance host.
                if profile["valid"]:
                    assert profile["top_self"]
                else:
                    assert profile["invalid_functions"] > 0
                    assert profile["self_cpu_ms"] is None and profile["top_self"] == []
            with pytest.raises(RuntimeError, match="ValueError"):
                await consumer.batch(instances["rust"].url, 17, 8192)
            assert consumer.process.returncode is None
        finally:
            await consumer.close()
        assert consumer.process.returncode == 0
    with experiment() as instances:
        asyncio.run(check(instances))


def test_connection_identity_does_not_confuse_a_recycled_descriptor():
    class Stream:
        def __init__(self):
            self.socket = SimpleNamespace(fileno=lambda: 42)

        def get_extra_info(self, name):
            return self.socket

    async def check():
        observer = benchmark.ConsumerConnections()
        first, second = Stream(), Stream()
        observer.begin()
        await observer.response(SimpleNamespace(extensions={"network_stream": first}))
        assert observer.snapshot()["response_connection_ids"] == [1]
        observer.begin()
        await observer.response(SimpleNamespace(extensions={"network_stream": second}))
        assert observer.snapshot()["response_connection_ids"] == [2]
        assert observer.snapshot()["reused_responses"] == 0
        observer.begin()
        await observer.response(SimpleNamespace(extensions={"network_stream": second}))
        await observer.response(SimpleNamespace(extensions={}))
        assert observer.snapshot()["reused_responses"] == 1
        assert observer.snapshot()["unobserved_responses"] == 1
    asyncio.run(check())


@pytest.mark.parametrize("warmup", [False, True])
@pytest.mark.parametrize("eof", [False, True])
def test_completion_metadata_is_independent_of_warmup(monkeypatch, tmp_path, warmup, eof):
    async def acquisition(*args, **kwargs):
        return {"rounds": []}

    binary = tmp_path / "binary"
    binary.write_bytes(b"fixture")
    monkeypatch.setattr(benchmark, "binary_path", lambda: binary)
    monkeypatch.setattr(benchmark, "runtime_identity", lambda: {})
    monkeypatch.setattr(benchmark, "run", acquisition)
    args = SimpleNamespace(output=tmp_path / "out.json", select=[], consumer_process=True,
                           connection_warmup=warmup, complete_range=eof)
    asyncio.run(benchmark.audit(args))
    report = json.loads(args.output.read_text())
    assert report["connection_warmup"] == warmup
    assert report["consumer_completion"] == ("exact-range-eof" if eof else "historical-prefix-break")


@pytest.mark.skipif(not binary_path().is_file(), reason="release relay required")
@pytest.mark.parametrize("warmup", [False, True])
@pytest.mark.parametrize("eof", [False, True])
def test_warmup_and_eof_are_separable_on_a_persistent_connection(warmup, eof):
    async def check(instances):
        consumer = ProcessConsumer()
        try:
            await consumer.start()
            target = instances["python"].url + "/t4/media/file"
            if warmup:
                warmed = await consumer.batch(target, 4, 8192, True, complete_range=True)
                assert all(sample["passed"] for sample in warmed["samples"])
                assert warmed["consumer_connections"]["new_tcp_connections"] == 4
            measured = await consumer.batch(target, 4, 8192, True, complete_range=eof)
            evidence = measured["consumer_connections"]
            assert evidence["unobserved_responses"] == 0
            assert 1 <= evidence["unique_connections"] <= 4
            assert len(measured["samples"]) == 4 and all(s["passed"] for s in measured["samples"])
            assert evidence["reused_responses"] == (4 if warmup else 0)
            assert evidence["new_tcp_connections"] == (0 if warmup else 4)
            expected_idle = 4 if eof else (4 - evidence["unique_connections"] if warmup else 0)
            assert measured["consumer_pool_after"]["target_idle"] == expected_idle
            assert all(s["response_exhausted"] == eof for s in measured["samples"])
        finally:
            await consumer.close()
        assert consumer.process.returncode == 0

    with experiment() as instances:
        asyncio.run(check(instances))
