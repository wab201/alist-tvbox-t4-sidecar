"""Bounded HLS-sized Range workload with normal and deliberately slow log readers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from scripts.benchmark_rust_media import expected_hash, measured_load
from scripts.diagnose_rust_affinity import bind_threads
from scripts.diagnose_rust_calibrated import runtime_identity, verify_budget
from tests.rust.relay_support import binary_path, experiment


def log_delta(before, after):
    names = ("events", "event_build_ns", "format_lock_write_flush_ns")
    delta = {name: after[name] - before[name] for name in names}
    if any(value < 0 for value in delta.values()):
        raise ValueError("log counters regressed")
    delta["process_max_write_ms"] = after["max_format_lock_write_flush_ns"] / 1e6
    delta["mean_write_ms"] = delta["format_lock_write_flush_ns"] / max(1, delta["events"]) / 1e6
    return delta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--compare-async", action="store_true")
    parser.add_argument("--slow-sink-ms", type=float, default=20)
    args = parser.parse_args()
    journal = args.output.with_suffix(".rows.jsonl")
    if sys.platform != "linux" or args.repeats < 3:
        parser.error("Linux and >=3 rounds required")
    if not 1 <= args.slow_sink_ms <= 50:
        parser.error("slow sink must be bounded to 1..50 ms per log line")
    if args.output.exists() or journal.exists():
        parser.error("preserve prior evidence")
    cpus = sorted(os.sched_getaffinity(0))
    if len(cpus) < 4:
        parser.error("four CPUs required")
    layout = {"origin": [cpus[0]], "python": [cpus[-1]], "rust": [cpus[-1]], "consumer": cpus[1:-1]}
    rows = []
    report = {"scope": "small-range-log-sink-backpressure", "production_ready": False,
              "slow_sink_is_fault_injection": True, "slow_sink_ms": args.slow_sink_ms,
              "rows": rows, "cpu_layout": layout,
              "runtime": runtime_identity(), "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest()}
    size = 65536
    try:
        for repeat in range(args.repeats):
            sinks = (("normal", 0), ("slow", args.slow_sink_ms / 1000))
            if repeat % 2:
                sinks = sinks[::-1]
            for sink, delay in sinks:
                modes = ("sync", "async") if args.compare_async else ("sync",)
                if repeat % 2:
                    modes = modes[::-1]
                for logging in modes:
                    bind_threads(os.getpid(), cpus)
                    flags = ["--observe-log", *(["--async-log"] if logging == "async" else [])]
                    with experiment(trace=True, cpu_layout=layout, relay_args=flags,
                                    log_read_delay=delay) as instances:
                        bind_threads(os.getpid(), layout["consumer"])
                        verify_budget(instances, layout)
                        with httpx.Client(trust_env=False, timeout=5) as admin:
                            before = admin.get(instances["rust"].url + "/health").json()
                            row = measured_load(instances, "rust", concurrency=16, duration=3,
                                                size=size, expected=expected_hash(size), consumer_mode="async",
                                                log_settle_timeout=30)
                            deadline = time.monotonic() + 5
                            while True:
                                after = admin.get(instances["rust"].url + "/health").json()
                                if after["active"] == 0 and after["log_observation"]["events"] == after["requests"]:
                                    break
                                assert time.monotonic() < deadline, "logging did not settle"
                                time.sleep(.01)
                        row.update(repeat=repeat, sink=sink, logging=logging,
                                   log_metrics=log_delta(before["log_observation"], after["log_observation"]),
                                   health=after)
                        verify_budget(instances, layout)
                        assert row["hashes_match"] and after["errors"] == 0
                        assert row["trace_match_count"] == row["trace_expected_count"], "logs not drained"
                        if metrics := after.get("logging"):
                            assert metrics["pending"] == 0 and metrics["written"] == after["requests"]
                            assert all(metrics[key] == 0 for key in (
                                "write_errors", "dropped_complete", "dropped_upstream_error",
                                "dropped_cancelled", "disconnected",
                            )), "lost log events"
                        rows.append(row)
                        with journal.open("a", encoding="utf-8") as out:
                            out.write(json.dumps(row) + "\n")
                        print(json.dumps({key: row[key] for key in (
                            "repeat", "sink", "logging", "requests", "ttfb_p95_ms", "mib_per_second", "log_metrics",
                        )}), flush=True)
        report["completed"] = True
        report["summary"] = [{"sink": sink, "logging": logging,
                              "p95_ms": statistics.median(r["ttfb_p95_ms"] for r in rows
                                                         if r["sink"] == sink and r["logging"] == logging),
                              "throughput": statistics.median(r["mib_per_second"] for r in rows
                                                             if r["sink"] == sink and r["logging"] == logging)}
                             for sink, logging in sorted({(r["sink"], r["logging"]) for r in rows})]
    except BaseException as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        bind_threads(os.getpid(), cpus)
        report["verified_bytes"] = sum(row["bytes"] for row in rows)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
