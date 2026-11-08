"""Compare leader-only and whole-thread CPU pinning without changing either relay."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
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
import psutil

from scripts.benchmark_rust_media import expected_hash, gate, measured_load, transfer_load
from tests.rust.relay_support import MIB, binary_path, experiment

CONDITIONS = ("leader-only", "all-threads")


def thread_masks(pid):
    return {int(path.name): sorted(os.sched_getaffinity(int(path.name)))
            for path in Path(f"/proc/{pid}/task").iterdir()}


def bind_threads(pid, cpus):
    # Bind the creator and every existing worker. Later children inherit a bound mask.
    for _ in range(5):
        for tid in thread_masks(pid):
            try:
                os.sched_setaffinity(tid, cpus)
            except ProcessLookupError:
                pass
        current = thread_masks(pid)
        if current and all(mask == sorted(cpus) for mask in current.values()):
            return current
    raise RuntimeError("thread affinity did not converge")


def verify_masks(snapshot, pid, cpus, inherited, condition):
    assert pid in snapshot, "leader missing"
    for tid, mask in snapshot.items():
        expected = cpus if tid == pid or condition == "all-threads" else inherited
        assert mask == sorted(expected), "thread CPU budget changed"


def paired_effects(rows):
    results = []
    for concurrency in sorted({row["concurrency"] for row in rows}):
        for mode in ("origin", "python", "rust"):
            pairs = {}
            for row in rows:
                if row["mode"] != mode or row["concurrency"] != concurrency:
                    continue
                assert row["hashes_match"] is True, "unverified bytes"
                key = (row["repeat"], row["condition"])
                assert key not in pairs, "duplicate observation"
                pairs[key] = row
            repeats = sorted({key[0] for key in pairs})
            assert len(repeats) >= 3, "three paired rounds required"
            assert len(pairs) == 2 * len(repeats), "incomplete pairs"
            effects = []
            for repeat in repeats:
                old, new = (pairs[(repeat, condition)] for condition in CONDITIONS)
                effects.append({
                    "repeat": repeat,
                    "p95_change_ms": new["ttfb_p95_ms"] - old["ttfb_p95_ms"],
                    "throughput_ratio": new["mib_per_second"] / old["mib_per_second"],
                })
            results.append({
                "mode": mode, "concurrency": concurrency, "pairs": effects,
                "improved_rounds": sum(row["p95_change_ms"] < 0 for row in effects),
                "median_p95_change_ms": statistics.median(row["p95_change_ms"] for row in effects),
                "median_throughput_ratio": statistics.median(row["throughput_ratio"] for row in effects),
            })
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--duration", type=float, default=3)
    args = parser.parse_args()
    journal = args.output.with_suffix(".rows.jsonl")
    if sys.platform != "linux" or args.repeats < 3 or args.duration < 3:
        parser.error("Linux, >=3 rounds and >=3 seconds required")
    if args.output.exists() or journal.exists():
        parser.error("preserve existing evidence")
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 4:
        parser.error("four CPUs required")
    rows = []
    report = {
        "scope": "thread-affinity-single-factor-full-concurrency-history",
        "production_ready": False, "profile": False,
        "consumer": "original-async-with-full-sha256", "source": "original",
        "rows": rows, "repeats": args.repeats, "duration": args.duration,
        "cpus": allowed, "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
        "diagnostic_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    size, expected = 64 * MIB, expected_hash(64 * MIB)
    try:
        with ExitStack() as stack:
            # Start both conditions before binding the consumer, preserving the old inheritance.
            cases = {condition: stack.enter_context(experiment(trace=True)) for condition in CONDITIONS}
            for condition, instances in cases.items():
                for mode in ("origin", "python", "rust"):
                    pid = instances[mode].process.pid
                    cpus = [allowed[0 if mode == "origin" else -1]]
                    if condition == "all-threads":
                        bind_threads(pid, cpus)
                    else:
                        os.sched_setaffinity(pid, cpus)
            bind_threads(os.getpid(), allowed[1:-1])

            def snapshot(condition):
                result = {}
                for mode in ("origin", "python", "rust"):
                    pid = cases[condition][mode].process.pid
                    masks = thread_masks(pid)
                    verify_masks(masks, pid, [allowed[0 if mode == "origin" else -1]], allowed, condition)
                    result[mode] = masks
                result["consumer"] = thread_masks(os.getpid())
                verify_masks(result["consumer"], os.getpid(), allowed[1:-1], allowed, "all-threads")
                return result

            for condition, instances in cases.items():
                snapshot(condition)
                for mode in ("python", "rust"):
                    asyncio.run(transfer_load(
                        instances[mode].url + "/t4/media/file", concurrency=8,
                        duration=.1, size=size, expected=expected,
                        trace_prefix=f"warm-affinity-{mode}-{time.time_ns()}",
                    ))
            for concurrency in (1, 4, 8, 16):
                for repeat in range(args.repeats):
                    conditions = CONDITIONS if repeat % 2 == 0 else CONDITIONS[::-1]
                    order = ("origin", "python", "rust") if repeat % 2 == 0 else ("origin", "rust", "python")
                    for condition in conditions:
                        for mode in order:
                            before = snapshot(condition)
                            row = measured_load(
                                cases[condition], mode, concurrency=concurrency, duration=args.duration,
                                size=size, expected=expected, consumer_mode="async",
                            )
                            row.update(condition=condition, repeat=repeat,
                                       affinity_before=before, affinity_after=snapshot(condition))
                            rows.append(row)
                            with journal.open("a", encoding="utf-8") as output:
                                output.write(json.dumps(row) + "\n")
                            print(json.dumps({key: row[key] for key in (
                                "condition", "concurrency", "repeat", "mode", "requests",
                                "ttfb_p95_ms", "mib_per_second", "cpu_seconds_per_gib",
                            )}), flush=True)
            report["health"] = {}
            with httpx.Client(trust_env=False, timeout=5) as client:
                for condition, instances in cases.items():
                    deadline = time.monotonic() + 3
                    while True:
                        relay = client.get(instances["rust"].url + "/health").json()
                        source = client.get(instances["origin"].url + "/health").json()
                        if relay["active"] == relay["starting"] == source["active"] == 0:
                            break
                        assert time.monotonic() < deadline, "resources not reclaimed"
                        time.sleep(.01)
                    assert relay["errors"] == relay["hedge_started"] == 0
                    report["health"][condition] = relay
            report["paired_effects"] = paired_effects(rows)
            report["comparisons"] = {
                condition: gate([row for row in rows if row["condition"] == condition],
                                contracts_passed=False, repeats=args.repeats)
                for condition in CONDITIONS
            }
            report["completed"] = len(rows) == 24 * args.repeats
    except BaseException as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        bind_threads(os.getpid(), allowed)
        report["verified_bytes"] = sum(row["bytes"] for row in rows)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"completed": report["completed"], "verified_bytes": report["verified_bytes"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
