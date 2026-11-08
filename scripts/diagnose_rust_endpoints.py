"""Source fairness x consumer hash diagnostic. Never a replacement performance gate."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil

from scripts.benchmark_rust_media import expected_hash, measured_load, transfer_load
from scripts.diagnose_rust_convoy import attribute
from tests.rust.relay_support import MIB, binary_path, experiment


def summarize(rows):
    result = []
    for fair in (False, True):
        for offload in (False, True):
            modes = {mode: [row for row in rows if row["mode"] == mode
                            and row["fair_source"] == fair and row["hash_offload"] == offload]
                     for mode in ("python", "rust")}
            if not all(modes.values()):
                continue
            def middle(mode, key):
                return statistics.median(row[key] for row in modes[mode])
            result.append({
                "fair_source": fair, "hash_offload": offload,
                "rounds": len(modes["python"]),
                "python_p95_ms": middle("python", "ttfb_p95_ms"),
                "rust_p95_ms": middle("rust", "ttfb_p95_ms"),
                "p95_difference_ms": middle("rust", "ttfb_p95_ms")
                                     - middle("python", "ttfb_p95_ms"),
                "mean_nodes_ms": {
                    mode: {
                        name: statistics.mean(sample["spans_ms"][name] for row in modes[mode]
                                              for sample in row["attribution"]["samples"])
                        for name in modes[mode][0]["attribution"]["mean_spans_ms"]
                    } for mode in modes
                },
                "origin_loop_lag_p95_ms": {
                    mode: middle(mode, "origin_loop_lag_p95_ms") for mode in modes
                },
                "hashes_match": all(row["hashes_match"] for group in modes.values() for row in group),
            })
    return result


def paired_effects(rows):
    """Compare one changed factor within each round, not two group medians."""
    if not rows or any(row.get("hashes_match") is not True for row in rows):
        raise ValueError("complete verified endpoint rows required")
    keyed = {}
    for row in rows:
        key = row["repeat"], row["mode"], row["fair_source"], row["hash_offload"]
        if key in keyed:
            raise ValueError("duplicate endpoint condition in one round")
        keyed[key] = row
    repeats = sorted({row["repeat"] for row in rows})
    if len(repeats) < 3 or repeats != list(range(len(repeats))):
        raise ValueError("at least three consecutive endpoint rounds required")
    expected = {(repeat, mode, fair, offload) for repeat in repeats
                for mode in ("python", "rust") for fair in (False, True)
                for offload in (False, True)}
    if set(keyed) != expected:
        raise ValueError("complete balanced endpoint rounds required")
    effects = []
    for mode in ("python", "rust"):
        for factor, other in (("fair_source", "hash_offload"),
                              ("hash_offload", "fair_source")):
            for fixed in (False, True):
                pairs = []
                for repeat in repeats:
                    def cell(changed):
                        fair = changed if factor == "fair_source" else fixed
                        offload = changed if factor == "hash_offload" else fixed
                        return keyed[repeat, mode, fair, offload]
                    baseline, candidate = cell(False), cell(True)
                    pairs.append({
                        "repeat": repeat,
                        "baseline_p95_ms": baseline["ttfb_p95_ms"],
                        "candidate_p95_ms": candidate["ttfb_p95_ms"],
                        "p95_change_ms": candidate["ttfb_p95_ms"] - baseline["ttfb_p95_ms"],
                        "throughput_ratio": candidate["mib_per_second"]
                                            / baseline["mib_per_second"],
                    })
                effects.append({
                    "mode": mode, "factor": factor, "fixed": {other: fixed},
                    "rounds": len(pairs),
                    "lower_p95_rounds": sum(pair["p95_change_ms"] < 0 for pair in pairs),
                    "median_paired_p95_change_ms": statistics.median(
                        pair["p95_change_ms"] for pair in pairs
                    ),
                    "median_paired_throughput_ratio": statistics.median(
                        pair["throughput_ratio"] for pair in pairs
                    ),
                    "pairs": pairs,
                })
    return effects


def analyze_existing(source, output):
    if source.resolve() == output.resolve():
        raise ValueError("offline output must not replace its input")
    raw = source.read_bytes()
    report = json.loads(raw)
    if (report.get("scope") != "diagnostic-only-source-fairness-x-hash-offload"
            or report.get("audit_completed") is not True):
        raise ValueError("completed endpoint diagnostic required")
    analysis = {
        "scope": "offline-endpoint-paired-effects",
        "production_ready": False, "performance_gate": "not-applied",
        "input": {"name": source.name, "sha256": hashlib.sha256(raw).hexdigest()},
        "analyzer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "binary_sha256": report["binary_sha256"],
        "paired_effects": paired_effects(report["rows"]),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as target:
        json.dump(analysis, target, indent=2)
    return analysis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--analyze-existing", type=Path,
                        help="analyze a completed report offline; never starts relays or media requests")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--duration", type=float, default=3)
    parser.add_argument("--upstream-read-kib", type=int)
    parser.add_argument("--startup-fairness", action="store_true")
    args = parser.parse_args()
    if args.analyze_existing is not None:
        print(json.dumps(analyze_existing(args.analyze_existing, args.output)), flush=True)
        return 0
    if args.repeats < 3 or args.duration < 3:
        parser.error("three rounds and three seconds minimum")
    if args.upstream_read_kib is not None and (
        args.upstream_read_kib != 0 and not 8 <= args.upstream_read_kib <= 1024
    ):
        parser.error("invalid upstream read cap")
    journal = args.output.with_suffix(".rows.jsonl")
    if args.output.exists() or journal.exists():
        parser.error("preserve previous evidence")
    allowed = psutil.Process().cpu_affinity()
    if len(allowed) < 4:
        parser.error("four CPUs required")
    rows = []
    report = {
        "scope": "diagnostic-only-source-fairness-x-hash-offload",
        "production_ready": False, "performance_gate": "not-applied",
        "upstream_read_kib": args.upstream_read_kib,
        "startup_fairness": args.startup_fairness,
        "profile": True, "concurrency": 16, "object_mib": 64,
        "history": "8-stream-warmup-then-first-batch-16; not the full original matrix",
        "hash_backlog_limit": "one awaited chunk per consumer worker",
        "hash_parallelism": 1,
        "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "rust/media-relay/Cargo.toml", "rust/media-relay/Cargo.lock",
                "rust/media-relay/src/main.rs", "rust/media-relay/src/hedge.rs",
                "rust/media-relay/src/upstream.rs", "rust/media-relay/src/startup.rs",
                "scripts/diagnose_rust_endpoints.py", "scripts/benchmark_rust_media.py",
                "scripts/diagnose_rust_convoy.py", "tests/rust/relay_support.py",
            )
        }, "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    size = 64 * MIB
    expected = expected_hash(size)
    try:
        for repeat in range(args.repeats):
            conditions = [(False, False), (False, True), (True, False), (True, True)]
            shift = repeat % len(conditions)
            for fair, offload in conditions[shift:] + conditions[:shift]:
                psutil.Process().cpu_affinity(allowed)
                with experiment(
                    trace=True, profile=True, fair_source=fair,
                    upstream_read_kib=args.upstream_read_kib,
                    startup_fairness=args.startup_fairness,
                ) as instances:
                    for mode in ("origin", "python", "rust"):
                        psutil.Process(instances[mode].process.pid).cpu_affinity([
                            allowed[0 if mode == "origin" else -1],
                        ])
                    psutil.Process().cpu_affinity(allowed[1:-1])
                    for mode in ("python", "rust"):
                        asyncio.run(transfer_load(
                            instances[mode].url + "/t4/media/file", concurrency=8,
                            duration=.1, size=size, expected=expected,
                            trace_prefix=f"warm-endpoints-{mode}-{time.time_ns()}",
                        ))
                    for mode in (("python", "rust") if repeat % 2 == 0 else ("rust", "python")):
                        row = measured_load(
                            instances, mode, concurrency=16, duration=args.duration, size=size,
                            expected=expected, consumer_mode="async", profile=True,
                            hash_offload=offload,
                        )
                        row["attribution"] = attribute(row)
                        row.update(repeat=repeat, fair_source=fair, hash_offload=offload)
                        rows.append(row)
                        with journal.open("a", encoding="utf-8") as output:
                            output.write(json.dumps(row) + "\n")
                        print(json.dumps({key: row[key] for key in (
                            "repeat", "fair_source", "hash_offload", "mode", "requests",
                            "ttfb_p95_ms", "mib_per_second",
                        )}), flush=True)
                    with httpx.Client(trust_env=False, timeout=5) as client:
                        deadline = time.monotonic() + 3
                        while True:
                            source = client.get(instances["origin"].url + "/health").json()
                            rust = client.get(instances["rust"].url + "/health").json()
                            if source["active"] == rust["active"] == 0:
                                break
                            assert time.monotonic() < deadline, "requests not reclaimed"
                            time.sleep(.01)
                        assert rust["errors"] == rust["hedge_started"] == rust["starting"] == 0
    except BaseException as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        psutil.Process().cpu_affinity(allowed)
        report["audit_completed"] = "error" not in report and len(rows) == args.repeats * 8
        report["verified_bytes"] = sum(row["bytes"] for row in rows)
        report["comparisons"] = summarize(rows)
        if report["audit_completed"]:
            report["paired_effects"] = paired_effects(rows)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "audit_completed", "verified_bytes", "comparisons",
    )}), flush=True)
    return 0 if report["audit_completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
