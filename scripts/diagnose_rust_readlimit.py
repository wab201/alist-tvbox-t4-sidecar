"""Single-factor upstream read-cap audit; never changes the production gateway."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil

from scripts.benchmark_rust_media import expected_hash, gate, measured_load, transfer_load
from tests.rust.relay_support import MIB, binary_path, experiment

CONDITIONS = {
    "original": None, "adaptive": 0, "read64": 64,
    "priority": None, "read64-priority": 64,
}
STARTUP_CONDITIONS = {"priority", "read64-priority"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--contracts-xml", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--duration", type=float, default=3)
    parser.add_argument("--conditions", nargs="+", choices=CONDITIONS,
                        default=["original", "adaptive", "read64"])
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8, 16])
    args = parser.parse_args()
    if args.repeats < 3 or args.duration < 3 or set(args.concurrency) != {1, 4, 8, 16}:
        parser.error("preserve >=3 rounds, >=3 seconds and the 1/4/8/16 connection history")
    if len(set(args.conditions)) != len(args.conditions):
        parser.error("conditions must be unique")
    suites = list(ET.parse(args.contracts_xml).getroot().iter("testsuite"))
    contracts = {
        key: sum(int(suite.get(key, "0")) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    if not contracts["tests"] or contracts["failures"] or contracts["errors"]:
        parser.error("a successful contract receipt is required before performance tests")
    journal = args.output.with_suffix(".rows.jsonl")
    if args.output.exists() or journal.exists():
        parser.error("preserve previous evidence")
    allowed = psutil.Process().cpu_affinity()
    if len(allowed) < 4:
        parser.error("four CPUs required")
    rows = []
    report = {
        "scope": "actual-upstream-read-cap-single-factor",
        "production_ready": False, "consumer": "original-async-with-full-sha256",
        "source": "original", "profile": False, "read_conditions": CONDITIONS,
        "repeats": args.repeats, "duration": args.duration, "rows": rows,
        "contracts": contracts,
        "contracts_xml_sha256": hashlib.sha256(args.contracts_xml.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "rust/media-relay/Cargo.toml", "rust/media-relay/Cargo.lock",
                "rust/media-relay/src/main.rs", "rust/media-relay/src/hedge.rs",
                "rust/media-relay/src/startup.rs",
                "rust/media-relay/src/upstream.rs", "scripts/diagnose_rust_readlimit.py",
                "scripts/benchmark_rust_media.py", "tests/rust/relay_support.py",
            )
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    size = 64 * MIB
    expected = expected_hash(size)
    try:
        with ExitStack() as stack:
            cases = {
                condition: stack.enter_context(experiment(
                    trace=True, upstream_read_kib=CONDITIONS[condition],
                    startup_fairness=condition in STARTUP_CONDITIONS,
                ))
                for condition in args.conditions
            }
            for instances in cases.values():
                for mode in ("origin", "python", "rust"):
                    psutil.Process(instances[mode].process.pid).cpu_affinity([
                        allowed[0 if mode == "origin" else -1],
                    ])
            psutil.Process().cpu_affinity(allowed[1:-1])
            for instances in cases.values():
                for mode in ("python", "rust"):
                    asyncio.run(transfer_load(
                        instances[mode].url + "/t4/media/file", concurrency=8,
                        duration=.1, size=size, expected=expected,
                        trace_prefix=f"warm-readcap-{mode}-{time.time_ns()}",
                    ))
            for concurrency in args.concurrency:
                for repeat in range(args.repeats):
                    shift = repeat % len(args.conditions)
                    conditions = args.conditions[shift:] + args.conditions[:shift]
                    for condition in conditions:
                        instances = cases[condition]
                        order = ("origin", "python", "rust") if repeat % 2 == 0 else (
                            "origin", "rust", "python"
                        )
                        for mode in order:
                            row = measured_load(
                                instances, mode, concurrency=concurrency, duration=args.duration,
                                size=size, expected=expected, consumer_mode="async",
                            )
                            row.update(condition=condition, repeat=repeat)
                            rows.append(row)
                            with journal.open("a", encoding="utf-8") as output:
                                output.write(json.dumps(row) + "\n")
                            print(json.dumps({
                                key: row[key] for key in (
                                    "condition", "concurrency", "repeat", "mode", "requests",
                                    "ttfb_p95_ms", "mib_per_second", "cpu_seconds_per_gib",
                                )
                            }), flush=True)
            report["health"] = {}
            with httpx.Client(trust_env=False, timeout=5) as client:
                for condition, instances in cases.items():
                    deadline = time.monotonic() + 3
                    while True:
                        health = client.get(instances["rust"].url + "/health").json()
                        source = client.get(instances["origin"].url + "/health").json()
                        if health["active"] == source["active"] == 0:
                            break
                        assert time.monotonic() < deadline, "requests not reclaimed"
                        time.sleep(.01)
                    assert health["errors"] == health["hedge_started"] == health["starting"] == 0
                    if limit := CONDITIONS[condition]:
                        assert 0 < health["max_upstream_read"] <= limit * 1024
                    report["health"][condition] = health
    except BaseException as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        psutil.Process().cpu_affinity(allowed)
        report["verified_bytes"] = sum(row["bytes"] for row in rows)
        report["audit_completed"] = "error" not in report and len(rows) == (
            len(args.conditions) * len(args.concurrency) * args.repeats * 3
        )
        report["comparisons"] = {
            condition: gate(
                [row for row in rows if row["condition"] == condition],
                contracts_passed=True, repeats=args.repeats,
            ) for condition in args.conditions
        }
        pairs = [
            (baseline, candidate) for baseline, candidate in (
                ("adaptive", "read64"), ("original", "priority"), ("read64", "read64-priority")
            ) if {baseline, candidate}.issubset(args.conditions)
        ]
        if pairs and report["audit_completed"]:
            report["single_factor"] = []
            for baseline, candidate in pairs:
                for concurrency in args.concurrency:
                    def middle(condition, key):
                        return statistics.median(row[key] for row in rows
                                                 if row["condition"] == condition
                                                 and row["concurrency"] == concurrency
                                                 and row["mode"] == "rust")
                    report["single_factor"].append({
                        "baseline": baseline, "candidate": candidate,
                        "concurrency": concurrency,
                        "baseline_p95_ms": middle(baseline, "ttfb_p95_ms"),
                        "candidate_p95_ms": middle(candidate, "ttfb_p95_ms"),
                        "p95_change_ms": middle(candidate, "ttfb_p95_ms")
                                         - middle(baseline, "ttfb_p95_ms"),
                        "throughput_ratio": middle(candidate, "mib_per_second")
                                            / middle(baseline, "mib_per_second"),
                    })
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        key: report.get(key) for key in (
            "audit_completed", "verified_bytes", "comparisons", "single_factor",
        )
    }), flush=True)
    # Per-condition gates are reported without claiming that an experiment is a release.
    return 0 if report["audit_completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
