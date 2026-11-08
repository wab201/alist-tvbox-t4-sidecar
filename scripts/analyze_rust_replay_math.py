"""Offline accounting of replay evidence; never promotes a performance gate."""
from __future__ import annotations

import argparse
from collections import defaultdict
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path

MIB = 1024 ** 2
FACTORS = ("block", "complete_range", "warmup", "consumer_loop", "consumer_pool_layout",
           "concurrency", "per_stream_kib")


def number(value, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError("non-finite or invalid measurement")
    return value


def matrix_rank(rows):
    """Small exact design matrices only, not a statistical model fit."""
    if not rows:
        return 0
    matrix = [[Fraction(value) for value in row] for row in rows]
    if len({len(row) for row in matrix}) != 1:
        raise ValueError("ragged design matrix")
    rank = 0
    for column in range(len(matrix[0])):
        pivot = next((i for i in range(rank, len(matrix)) if matrix[i][column]), None)
        if pivot is None:
            continue
        matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
        divisor = matrix[rank][column]
        matrix[rank] = [v / divisor for v in matrix[rank]]
        for i in range(rank + 1, len(matrix)):
            multiplier = matrix[i][column]
            matrix[i] = [a - multiplier * b for a, b in zip(matrix[i], matrix[rank])]
        rank += 1
        if rank == len(matrix):
            break
    return rank


def pool_change(before, after):
    """Symmetric two-factor identity: delta(n*c), with no causal interpretation."""
    n0, n1 = before["pool_calls"], after["pool_calls"]
    if not n0 or not n1:
        return None
    c0, c1 = before["pool_cpu_ms"] / n0, after["pool_cpu_ms"] / n1
    frequency = (n1 - n0) * (c0 + c1) / 2
    cost = (c1 - c0) * (n0 + n1) / 2
    return {"calls_before": n0, "calls_after": n1,
            "cpu_us_per_call_before": c0 * 1000, "cpu_us_per_call_after": c1 * 1000,
            "frequency_component_ms": frequency, "cost_component_ms": cost,
            "delta_pool_cpu_ms": after["pool_cpu_ms"] - before["pool_cpu_ms"],
            "closure_error_ms": after["pool_cpu_ms"] - before["pool_cpu_ms"] - frequency - cost}


def runtime_summary(timing):
    if timing.get("valid") is not True or timing.get("restored") is not True:
        raise ValueError("invalid runtime observer")
    start, end = number(timing["started_ns"]), number(timing["finished_ns"])
    wall, cpu = number(timing["wall_ns"], positive=True), number(timing["main_cpu_ns"])
    if end - start != wall or cpu > wall:
        raise ValueError("inconsistent main-thread window")
    pools, gc = timing["pools"], timing["gc"]
    if timing.get("foreign_pool_calls") != 0 or gc.get("truncated") is not False:
        raise ValueError("incomplete runtime observer")
    if any(p["errors"] for p in pools) or gc.get("errors"):
        raise ValueError("runtime observer errors")
    pool_cpu = sum(number(p["cpu_ns"]) for p in pools)
    if pool_cpu > cpu:
        raise ValueError("pool CPU exceeds owner CPU")
    return {"window_ms": wall / 1e6, "main_cpu_ms": cpu / 1e6,
            "non_cpu_ms": (wall - cpu) / 1e6, "cpu_fraction": cpu / wall,
            "pool_cpu_ms": pool_cpu / 1e6,
            "pool_calls": sum(number(p["calls"]) for p in pools),
            "outside_pool_cpu_ms": (cpu - pool_cpu) / 1e6,
            "gc_wall_ms_inclusive": number(gc["wall_ns"]) / 1e6,
            "pool_zero_cost_fixed_path_speedup_ceiling": wall / (wall - pool_cpu),
            "runqueue_identifiable": timing.get("runqueue_reliable") is True,
            "limitation": "observed window; GC overlaps pool; non-CPU is not kernel runqueue time"}


def clock_summary(clock, batch_ms):
    if clock.get("schema") != "nested-batch-cpu-clocks-v1" or clock.get("valid") is not True:
        raise ValueError("invalid batch clock window")
    wall, cpu = number(clock["wall_ns"]), number(clock["main_cpu_ns"])
    if (clock["finished_ns"] - clock["started_ns"] != wall or cpu > wall
            or wall / 1e6 < batch_ms):
        raise ValueError("invalid nested clock intervals")
    return {"window_ms": wall / 1e6, "main_cpu_ms": cpu / 1e6,
            "non_cpu_plus_boundary_ms": (wall - cpu) / 1e6,
            "clock_envelope_ms": wall / 1e6 - batch_ms,
            "process_cpu_ms": number(clock["process_cpu_ns"]) / 1e6,
            "main_clock_resolution_ns": number(clock["main_clock_resolution_ns"], positive=True)}


def row_summary(row, capture):
    if not row.get("passed") or not row.get("connection_evidence_passed"):
        raise ValueError("failed byte or connection evidence")
    if row["mode"] not in {"python", "rust", "python_aa"}:
        raise ValueError("unsupported mode")
    samples = row["samples"]
    if not samples or len(samples) != row["concurrency"]:
        raise ValueError("incomplete samples")
    for sample in samples:
        if (sample.get("passed") is not True or sample.get("sha256") != capture["sha256"]
                or sample["bytes"] != capture["bytes"]
                or sample["response_exhausted"] != row["complete_range"]):
            raise ValueError("unmatched byte contract")
        h, f, e = (number(sample[k]) for k in ("headers_ms", "ttfb_ms", "elapsed_ms"))
        if not 0 <= h <= f <= e:
            raise ValueError("invalid per-request phases")
    elapsed = number(row["seconds"], positive=True)
    size = sum(number(s["bytes"], positive=True) for s in samples)
    rate = number(row["mib_per_second"], positive=True)
    if size != row["bytes"] or not math.isclose(size / MIB / rate, elapsed, rel_tol=1e-9):
        raise ValueError("bytes/rate/time identity failed")
    # Use the SAME request for both terms, not p95(A) + p95(B).
    index = sorted(range(len(samples)), key=lambda i: samples[i]["ttfb_ms"])[
        int((len(samples) - 1) * .95 + .5)]
    critical = samples[index]
    if not math.isclose(critical["ttfb_ms"], row["ttfb_p95_ms"], rel_tol=1e-9):
        raise ValueError("percentile convention mismatch")
    result = {"repeat": row["repeat"], "mode": row["mode"], "batch_ms": elapsed * 1000,
              "bytes": size, "concurrency": row["concurrency"], "mib_per_second": rate,
              "instrumented": bool(row.get("runtime_timing") or row.get("profile")),
              "ttfb_p95_ms": critical["ttfb_ms"], "p95_request_index": index,
              "p95_request_headers_ms": critical["headers_ms"],
              "p95_request_header_to_body_ms": critical["ttfb_ms"] - critical["headers_ms"],
              "process_cpu_ms_coarse": {"consumer": number(row["consumer_cpu_seconds"]) * 1000,
                  **{k: number(row["owner_cpu_seconds"][k]) * 1000 for k in ("origin", "gateway", "relay")}}}
    if row.get("runtime_timing"):
        result["runtime"] = {owner: runtime_summary(row[owner + "_runtime_timing"])
                             for owner in ("consumer", "gateway")}
        g, c = (row[k + "_runtime_timing"] for k in ("gateway", "consumer"))
        if not g["started_ns"] <= c["started_ns"] < c["finished_ns"] <= g["finished_ns"]:
            raise ValueError("non-nested owner windows")
        result["gateway_extra_window_ms"] = (g["wall_ns"] - c["wall_ns"]) / 1e6
    if "consumer_clock_window" in row:
        result["clock"] = clock_summary(row["consumer_clock_window"], elapsed * 1000)
    return result


def pair(before, after):
    if before["bytes"] != after["bytes"] or before["concurrency"] != after["concurrency"]:
        raise ValueError("unequal paired work")
    delta = after["batch_ms"] - before["batch_ms"]
    ratio = before["batch_ms"] / after["batch_ms"]
    ttfb_delta = after["ttfb_p95_ms"] - before["ttfb_p95_ms"]
    limit = max(2, before["ttfb_p95_ms"] * .1)
    observed = before["instrumented"] or after["instrumented"]
    result = {"repeat": before["repeat"], "delta_batch_ms": delta, "throughput_ratio": ratio,
              "ttfb_delta_ms": ttfb_delta, "ttfb_limit_ms": limit,
              "ttfb_header_component_ms": after["p95_request_headers_ms"] - before["p95_request_headers_ms"],
              "ttfb_body_component_ms": after["p95_request_header_to_body_ms"] - before["p95_request_header_to_body_ms"],
              "saving_needed_for_throughput_gate_ms": max(0, after["batch_ms"] - before["batch_ms"] / .95),
              "performance_passed": None if observed else ratio >= .95 and ttfb_delta <= limit,
              "instrumented": observed}
    if "runtime" in before and "runtime" in after:
        owners = {}
        for owner in ("consumer", "gateway"):
            b, a = before["runtime"][owner], after["runtime"][owner]
            differences = {"delta_" + field: a[field] - b[field] for field in (
                "window_ms", "main_cpu_ms", "non_cpu_ms", "pool_cpu_ms", "outside_pool_cpu_ms")}
            differences["closure_error_ms"] = (differences["delta_window_ms"]
                - differences["delta_main_cpu_ms"] - differences["delta_non_cpu_ms"])
            differences["pool_factors"] = pool_change(b, a)
            owners[owner] = differences
        result["runtime_decomposition"] = owners
    if "clock" in before and "clock" in after:
        c = {"delta_" + k: after["clock"][k] - before["clock"][k] for k in (
            "main_cpu_ms", "non_cpu_plus_boundary_ms", "clock_envelope_ms")}
        c["closure_error_ms"] = (delta - c["delta_main_cpu_ms"]
            - c["delta_non_cpu_plus_boundary_ms"] + c["delta_clock_envelope_ms"])
        result["consumer_clock_decomposition"] = c
    return result


def analyze(report):
    if report.get("scope") != "captured-media-local-replay-attribution" or report.get("complete") is not True:
        raise ValueError("complete controlled replay required")
    capture = report["capture"]
    if capture.get("status") != "pass":
        raise ValueError("invalid capture")
    strata = defaultdict(list)
    for row in report["rows"]:
        reference = capture
        if report.get("factorial_study"):
            size = row["per_stream_kib"] * 1024
            reference = report["prefix_references"][str(size)]
            if (reference["bytes"] != size or reference["status"] != 206
                    or reference["range_metadata"] != [0, size - 1, capture["bytes"]]
                    or (size == capture["bytes"] and reference["sha256"] != capture["sha256"])):
                raise ValueError("invalid captured-prefix evidence")
        strata[tuple(row.get(k, "unrecorded") for k in FACTORS)].append(row_summary(row, reference))
    output = []
    for factors, rows in strata.items():
        cells = {}
        sequences = defaultdict(list)
        for row in rows:
            key = row["repeat"], row["mode"], row["instrumented"]
            if key in cells:
                raise ValueError("duplicate evidence cell")
            cells[key] = row
            if not row["instrumented"]:
                sequences[row["repeat"]].append(row["mode"])
        if any(sorted(order) != ["python", "python_aa", "rust"] for order in sequences.values()):
            raise ValueError("incomplete three-arm block")
        design, positions = [], {m: [0, 0, 0] for m in ("python", "rust", "python_aa")}
        for order in sequences.values():
            for position, mode in enumerate(order):
                positions[mode][position] += 1
                design.append([1, int(mode == "rust"), int(mode == "python_aa"),
                               int(position == 1), int(position == 2)])
        pairs = {"rust_vs_python": [], "aa_vs_python": [], "observed_rust_vs_python": []}
        for repeat in sorted(sequences):
            b = cells[repeat, "python", False]
            pairs["rust_vs_python"].append(pair(b, cells[repeat, "rust", False]))
            pairs["aa_vs_python"].append(pair(b, cells[repeat, "python_aa", False]))
            if (repeat, "python", True) in cells and (repeat, "rust", True) in cells:
                pairs["observed_rust_vs_python"].append(pair(
                    cells[repeat, "python", True], cells[repeat, "rust", True]))
        output.append({"factors": dict(zip(FACTORS, factors)), "rows": rows, **pairs,
                       "order_design": {"rank": matrix_rank(design), "columns": 5,
                           "positions": positions,
                           "rust_equals_middle_column": all(row[1] == row[3] for row in design)}})
    rows = [row for group in output for row in group["rows"] if not row["instrumented"]]
    workload = [[1, r["concurrency"], r["concurrency"] ** 2, Fraction(r["bytes"], MIB)] for r in rows]
    result = {"row_count": len(report["rows"]), "capture_sha256": capture["sha256"],
              "workload_design": {"columns": ["intercept", "N", "N_squared", "total_MiB"],
                                  "rank": matrix_rank(workload)}, "strata": output}
    if report.get("factorial_study"):
        result["workload_contrasts"] = workload_contrasts(rows)
    return result


def workload_contrasts(rows):
    """Finite differences of T=a+b*N+c*N^2+d*bytes, not causal regression."""
    expected = {(mode, repeat, n, kib) for mode in ("python", "rust", "python_aa")
                for repeat in range(3) for n in (1, 4, 8, 16) for kib in (64, 256, 1024)}
    cells = {(r["mode"], r["repeat"], r["concurrency"],
              r["bytes"] // r["concurrency"] // 1024): r for r in rows}
    if len(rows) != len(expected) or set(cells) != expected:
        raise ValueError("complete workload matrix required")
    metrics = ("batch_ms", "p95_request_headers_ms", "main_cpu_ms", "non_cpu_plus_boundary_ms")
    result = {"assumption": "fixed path additive polynomial; contrasts are observations, not causal coefficients",
              "byte_slopes": [], "concurrency_curvature": [], "header_body_interaction": []}
    for mode in ("python", "rust", "python_aa"):
        for repeat in range(3):
            def value(n, kib, metric):
                row = cells[mode, repeat, n, kib]
                return number(row.get(metric, row.get("clock", {}).get(metric)))
            for n in (1, 4, 8, 16):
                for metric in metrics:
                    low, mid, high = (value(n, kib, metric) for kib in (64, 256, 1024))
                    slope = (high - low) / (n * (1024 - 64) / 1024)
                    result["byte_slopes"].append({"mode": mode, "repeat": repeat, "concurrency": n,
                        "metric": metric, "ms_per_total_MiB": slope,
                        "midpoint_residual_ms": mid - (low + .2 * (high - low))})
            for kib in (64, 256, 1024):
                for metric in metrics:
                    times = [value(n, kib, metric) for n in (1, 4, 8, 16)]
                    slopes = [(times[i + 1] - times[i]) / dn for i, dn in enumerate((3, 4, 8))]
                    result["concurrency_curvature"].append({"mode": mode, "repeat": repeat,
                        "per_stream_kib": kib, "metric": metric,
                        "quadratic_low_ms_per_request_squared": (slopes[1] - slopes[0]) / 7,
                        "quadratic_high_ms_per_request_squared": (slopes[2] - slopes[1]) / 12})
            def header_growth(n):
                return value(n, 1024, "p95_request_headers_ms") - value(n, 64, "p95_request_headers_ms")
            result["header_body_interaction"].append({"mode": mode, "repeat": repeat,
                "header_growth_single_ms": header_growth(1), "header_growth_16_ms": header_growth(16),
                "difference_in_differences_ms": header_growth(16) - header_growth(1)})
    return result


def micro_summary(report):
    rows = {(r["case"], r["repeat"], r.get("elide", r.get("observed"))): r for r in report["rows"]}
    estimates = []
    for repeat in range(3):
        before = number(rows["warm-idle16", repeat, False]["ns_per_call"], positive=True)
        after = number(rows["warm-idle16", repeat, True]["ns_per_call"], positive=True)
        estimates.append({"repeat": repeat, "saving_us_per_call": (before - after) / 1000,
                          "hypothetical_97_calls_saving_ms": (before - after) * 97 / 1e6})
    return {"scan_estimates": estimates,
            "assumption": "all 97 calls behave like the isolated idle16 microbenchmark; not a causal bound"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--micro", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = {"scope": "offline-replay-mathematical-accounting", "production_ready": False,
              "limitations": ["no causal regression or performance correction",
                  "process CPU has coarse clocks and unequal owner windows; do not sum for latency",
                  "non-CPU residual includes I/O, event-loop idleness and scheduling; not attribution",
                  "instrumented rows never pass a performance gate", "p95 components use the same request"],
              "reports": []}
    for path in args.inputs:
        raw = path.read_bytes()
        result["reports"].append({"file": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
                                  **analyze(json.loads(raw))})
    if args.micro:
        raw = args.micro.read_bytes()
        result["micro"] = {"file": args.micro.name, "sha256": hashlib.sha256(raw).hexdigest(),
                           **micro_summary(json.loads(raw))}
    with args.output.open("x", encoding="utf-8") as out:
        json.dump(result, out, ensure_ascii=False, indent=2, allow_nan=False)
        out.write("\n")
    print(json.dumps({"reports": len(result["reports"]),
                      "rows": sum(r["row_count"] for r in result["reports"]), "production_ready": False}))


if __name__ == "__main__":
    main()
