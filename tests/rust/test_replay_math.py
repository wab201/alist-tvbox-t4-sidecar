from copy import deepcopy
import json
import sys

import pytest

from scripts import analyze_rust_replay_math as model


def evidence():
    rows = []
    for repeat in range(3):
        order = ("python", "rust", "python_aa") if repeat % 2 == 0 else ("python_aa", "rust", "python")
        for mode in order:
            samples = [{"passed": True, "sha256": "a" * 64, "bytes": model.MIB,
                        "response_exhausted": True, "headers_ms": 10 + i,
                        "ttfb_ms": 11 + i, "elapsed_ms": 40 + i} for i in range(16)]
            rows.append({"block": "16k", "complete_range": True, "warmup": True,
                         "consumer_loop": "asyncio", "consumer_pool_layout": "shared",
                         "mode": mode, "repeat": repeat, "concurrency": 16, "samples": samples,
                         "passed": True, "connection_evidence_passed": True,
                         "bytes": 16 * model.MIB, "seconds": .1, "mib_per_second": 160,
                         "ttfb_p95_ms": 25, "consumer_cpu_seconds": .08,
                         "owner_cpu_seconds": {"origin": .02, "gateway": .05, "relay": .01}})
    return {"scope": "captured-media-local-replay-attribution", "complete": True,
            "capture": {"status": "pass", "bytes": model.MIB, "sha256": "a" * 64}, "rows": rows}


def timing(cpu=60_000_000, pool=20_000_000, calls=97):
    return {"valid": True, "restored": True, "started_ns": 10, "finished_ns": 100_000_010,
            "wall_ns": 100_000_000, "main_cpu_ns": cpu, "foreign_pool_calls": 0,
            "pools": [{"calls": calls, "cpu_ns": pool, "errors": {}}],
            "gc": {"wall_ns": 2_000_000, "truncated": False}, "runqueue_reliable": False}


def test_rank_exposes_fixed_middle_alias_and_fixed_workload():
    result = model.analyze(evidence())
    assert result["workload_design"]["rank"] == 1
    order = result["strata"][0]["order_design"]
    assert order["rank"] == 4 and order["columns"] == 5
    assert order["rust_equals_middle_column"]
    assert order["positions"]["rust"] == [0, 3, 0]
    from scripts.benchmark_rust_selected_media import batch_modes
    design = [[1, int(m == "rust"), int(m == "python_aa"), int(p == 1), int(p == 2)]
              for r in range(3) for p, m in enumerate(batch_modes(r, aa=True))]
    assert model.matrix_rank(design) == 5
    workload = [[1, n, n * n, n * size] for n in (1, 4, 8, 16) for size in (1, 4, 16)]
    assert model.matrix_rank(workload) == 4


def test_percentile_components_use_same_request_not_separate_quantiles():
    report = evidence()
    # Largest header delay is NOT on the request defining p95 TTFB.
    for row in report["rows"]:
        row["samples"][14]["headers_ms"] = 1
    row = model.analyze(report)["strata"][0]["rows"][0]
    assert row["p95_request_index"] == 14
    assert row["p95_request_headers_ms"] == 1
    assert row["p95_request_header_to_body_ms"] == 24


def test_gate_is_per_pair_and_probes_never_pass():
    base = model.row_summary(evidence()["rows"][0], evidence()["capture"])
    slower = {**base, "batch_ms": 120, "mib_per_second": 16 / .12}
    result = model.pair(base, slower)
    assert result["throughput_ratio"] == pytest.approx(5 / 6)
    assert result["saving_needed_for_throughput_gate_ms"] == pytest.approx(120 - 100 / .95)
    assert result["performance_passed"] is False
    assert model.pair(base, {**slower, "instrumented": True})["performance_passed"] is None
    with pytest.raises(ValueError, match="unequal"):
        model.pair(base, {**base, "bytes": 1})


def test_runtime_delta_closes_and_pool_frequency_cost_are_not_double_counted():
    b, a = model.runtime_summary(timing()), model.runtime_summary(timing(cpu=40_000_000, pool=10_000_000, calls=96))
    base = model.row_summary(evidence()["rows"][0], evidence()["capture"])
    result = model.pair({**base, "runtime": {"consumer": b, "gateway": b}, "instrumented": True},
                        {**base, "runtime": {"consumer": a, "gateway": a}, "instrumented": True})
    delta = result["runtime_decomposition"]["consumer"]
    assert delta["delta_main_cpu_ms"] == -20 and delta["delta_non_cpu_ms"] == 20
    assert abs(delta["closure_error_ms"]) < 1e-10
    assert abs(delta["pool_factors"]["closure_error_ms"]) < 1e-10
    assert b["outside_pool_cpu_ms"] == 40  # GC already overlaps; do not subtract again.
    assert b["pool_zero_cost_fixed_path_speedup_ceiling"] == 1.25
    assert not b["runqueue_identifiable"]
    assert model.pool_change({"pool_calls": 0}, a) is None


@pytest.mark.parametrize("fault", ["missing", "duplicate", "byte_hash", "rate", "percentile", "phase", "incomplete"])
def test_rejects_inconsistent_evidence(fault):
    report = evidence()
    if fault == "missing":
        report["rows"].pop()
    elif fault == "duplicate":
        report["rows"].append(deepcopy(report["rows"][0]))
    elif fault == "byte_hash":
        report["rows"][0]["samples"][0]["sha256"] = "b" * 64
    elif fault == "rate":
        report["rows"][0]["mib_per_second"] = 1
    elif fault == "percentile":
        report["rows"][0]["ttfb_p95_ms"] = 99
    elif fault == "phase":
        report["rows"][0]["samples"][0]["headers_ms"] = 99
    else:
        report["complete"] = False
    with pytest.raises(ValueError):
        model.analyze(report)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1, "123"])
def test_nonfinite_or_nonnumeric_data_is_not_a_zero_or_pass(value):
    with pytest.raises(ValueError):
        model.number(value)


@pytest.mark.parametrize("field,value", [("main_cpu_ns", 101_000_000), ("wall_ns", 90),
                                          ("restored", False), ("foreign_pool_calls", 1)])
def test_rejects_invalid_runtime_windows(field, value):
    data = timing()
    data[field] = value
    with pytest.raises(ValueError):
        model.runtime_summary(data)


def test_cli_whitelists_output_and_refuses_to_overwrite(tmp_path, monkeypatch):
    source, output = tmp_path / "source.json", tmp_path / "\u6570\u5b66.json"
    report = evidence()
    report["private_url"] = "https://private.invalid/?token=secret"
    report["rows"][0]["headers"] = {"Authorization": "secret"}
    del report["rows"][0]["consumer_loop"]
    # Missing historical metadata remains explicitly unknown, never inferred.
    for row in report["rows"]:
        row.pop("consumer_loop", None)
    source.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["analyze", str(source), "--output", str(output)])
    model.main()
    saved = output.read_text(encoding="utf-8")
    assert "secret" not in saved and "private.invalid" not in saved
    result = json.loads(saved)
    assert result["production_ready"] is False
    assert result["reports"][0]["strata"][0]["factors"]["consumer_loop"] == "unrecorded"
    with pytest.raises(FileExistsError):
        model.main()


def test_micro_estimate_stays_a_conditional_estimate():
    rows = [{"case": "warm-idle16", "repeat": r, "elide": elide,
             "ns_per_call": 200_000 if elide else 250_000}
            for r in range(3) for elide in (False, True)]
    result = model.micro_summary({"rows": rows})
    assert result["scan_estimates"][0]["hypothetical_97_calls_saving_ms"] == 4.85
    assert "not a causal bound" in result["assumption"]


def test_nested_clock_decomposition_closes_without_calling_residual_runqueue():
    base = model.row_summary(evidence()["rows"][0], evidence()["capture"])
    def clock(wall, cpu):
        return {"schema": "nested-batch-cpu-clocks-v1", "valid": True,
                "started_ns": 10, "finished_ns": 10 + wall, "wall_ns": wall,
                "main_cpu_ns": cpu, "process_cpu_ns": cpu, "main_clock_resolution_ns": 1}
    b = model.clock_summary(clock(101_000_000, 80_000_000), 100)
    a = model.clock_summary(clock(122_000_000, 60_000_000), 120)
    change = model.pair({**base, "clock": b}, {**base, "clock": a, "batch_ms": 120})
    c = change["consumer_clock_decomposition"]
    assert c["delta_main_cpu_ms"] == -20
    assert c["delta_non_cpu_plus_boundary_ms"] == 41
    assert c["delta_clock_envelope_ms"] == 1
    assert abs(c["closure_error_ms"]) < 1e-10
    with pytest.raises(ValueError, match="nested"):
        model.clock_summary(clock(99_000_000, 80_000_000), 100)


def factorial_evidence():
    from scripts.diagnose_rust_replay import factorial_conditions, factorial_order
    from scripts.benchmark_rust_selected_media import batch_modes

    report = evidence()
    report["rows"] = []
    report["factorial_study"] = True
    report["prefix_references"] = {str(kib * 1024): {
        "bytes": kib * 1024, "sha256": report["capture"]["sha256"] if kib == 1024 else f"{kib:064x}",
        "status": 206, "range_metadata": [0, kib * 1024 - 1, model.MIB], "mime": "video/mp2t"}
        for kib in (64, 256, 1024)}
    for repeat in range(3):
        for index, n, kib in factorial_order(repeat):
            for mode in batch_modes((repeat + index) % 3 + 3 * (index % 2), aa=True):
                row = deepcopy(evidence()["rows"][0])
                ms = 2 + 3 * n + .4 * n ** 2 + 5 * n * kib / 1024
                row.update(mode=mode, repeat=repeat, concurrency=n, per_stream_kib=kib,
                           bytes=n * kib * 1024, seconds=ms / 1000,
                           mib_per_second=n * kib / 1024 / (ms / 1000),
                           consumer_clock_window={"schema": "nested-batch-cpu-clocks-v1", "valid": True,
                               "started_ns": 0, "finished_ns": round((ms + .01) * 1e6),
                               "wall_ns": round((ms + .01) * 1e6), "main_cpu_ns": round(.6 * ms * 1e6),
                               "process_cpu_ns": round(.6 * ms * 1e6), "main_clock_resolution_ns": 1})
                ref = report["prefix_references"][str(kib * 1024)]
                row["samples"] = [{"passed": True, "sha256": ref["sha256"], "bytes": ref["bytes"],
                    "response_exhausted": True, "headers_ms": .2 * ms + i * .01,
                    "ttfb_ms": .21 * ms + i * .01, "elapsed_ms": .9 * ms} for i in range(n)]
                row["ttfb_p95_ms"] = row["samples"][int((n - 1) * .95 + .5)]["ttfb_ms"]
                report["rows"].append(row)
    assert len(factorial_conditions()) == 12
    return report


def test_full_factorial_analysis_preserves_pairs_and_recovers_known_contrasts():
    result = model.analyze(factorial_evidence())
    assert result["row_count"] == 108 and result["workload_design"]["rank"] == 4
    assert len(result["strata"]) == 12
    assert all(s["order_design"]["rank"] == 5 for s in result["strata"])
    assert all(p["performance_passed"] for s in result["strata"] for p in s["rust_vs_python"])
    contrasts = result["workload_contrasts"]
    for row in contrasts["byte_slopes"]:
        if row["metric"] == "batch_ms":
            assert row["ms_per_total_MiB"] == pytest.approx(5)
            assert row["midpoint_residual_ms"] == pytest.approx(0, abs=1e-10)
    for row in contrasts["concurrency_curvature"]:
        if row["metric"] == "batch_ms":
            assert row["quadratic_low_ms_per_request_squared"] == pytest.approx(.4)
            assert row["quadratic_high_ms_per_request_squared"] == pytest.approx(.4)
    assert all(r["difference_in_differences_ms"] == pytest.approx(15 * .9375)
               for r in contrasts["header_body_interaction"])


@pytest.mark.parametrize("fault", ["prefix_hash", "prefix_range", "missing_cell", "duplicate_cell"])
def test_factorial_analysis_rejects_cross_prefix_or_incomplete_data(fault):
    report = factorial_evidence()
    if fault == "prefix_hash":
        report["prefix_references"]["65536"]["sha256"] = "bad"
    elif fault == "prefix_range":
        report["prefix_references"]["65536"]["range_metadata"] = [0, 65535, 65536]
    elif fault == "missing_cell":
        report["rows"] = [r for r in report["rows"] if r["concurrency"] != 4 or r["per_stream_kib"] != 256]
    else:
        report["rows"].append(deepcopy(report["rows"][0]))
    with pytest.raises(ValueError):
        model.analyze(report)


def test_midpoint_residual_exposes_non_linear_byte_cost_instead_of_forcing_a_fit():
    result = model.analyze(factorial_evidence())
    rows = [r for s in result["strata"] for r in s["rows"]]
    selected = next(r for r in rows if r["mode"] == "rust" and r["repeat"] == 0
                    and r["concurrency"] == 16 and r["bytes"] == 16 * 256 * 1024)
    selected["batch_ms"] += 50
    changed = model.workload_contrasts(rows)
    slope = next(r for r in changed["byte_slopes"] if r["mode"] == "rust" and r["repeat"] == 0
                 and r["concurrency"] == 16 and r["metric"] == "batch_ms")
    assert slope["ms_per_total_MiB"] == pytest.approx(5)
    assert slope["midpoint_residual_ms"] == pytest.approx(50)
