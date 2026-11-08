"""Same-instance paired, event-driven first-byte admission; no dispatch sleeps."""
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
from scripts.diagnose_rust_calibrated import verify_budget
from tests.rust.relay_support import MIB, binary_path, experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    journal = args.output.with_suffix(".rows.jsonl")
    if sys.platform != "linux" or args.repeats < 3 or args.output.exists() or journal.exists():
        parser.error("Linux, >=3 rounds and fresh output required")
    cpus = sorted(os.sched_getaffinity(0))
    if len(cpus) < 4:
        parser.error("four CPUs required")
    layout = {"origin": [cpus[0]], "python": [cpus[-1]], "rust": [cpus[-1]], "consumer": cpus[1:-1]}
    rows = []
    report = {"scope": "same-instance-startup-admission", "production_ready": False,
              "rows": rows, "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest()}
    try:
        for limit in (4, 8):
            bind_threads(os.getpid(), cpus)
            with experiment(trace=True, cpu_layout=layout, relay_args=("--startup-limit", str(limit))) as instances:
                bind_threads(os.getpid(), layout["consumer"])
                for repeat in range(args.repeats):
                    for enabled in ((False, True) if repeat % 2 == 0 else (True, False)):
                        # This is only a cold-connection reset between trials, not relay pacing.
                        time.sleep(5.5)
                        verify_budget(instances, layout)
                        row = measured_load(instances, "rust", concurrency=16, duration=3,
                                            size=64 * MIB, expected=expected_hash(64 * MIB),
                                            consumer_mode="async", admission_bypass=not enabled)
                        row.update(limit=limit, enabled=enabled, repeat=repeat)
                        verify_budget(instances, layout)
                        with httpx.Client(trust_env=False, timeout=5) as client:
                            health = client.get(instances["rust"].url + "/health").json()
                            assert health["active"] == health["errors"] == 0
                        rows.append(row)
                        with journal.open("a", encoding="utf-8") as out:
                            out.write(json.dumps(row) + "\n")
                        print(json.dumps({k: row[k] for k in (
                            "limit", "enabled", "repeat", "ttfb_p95_ms", "mib_per_second",
                        )}), flush=True)
        report["summary"] = []
        for limit in (4, 8):
            cells = {(r["repeat"], r["enabled"]): r for r in rows if r["limit"] == limit}
            deltas = [cells[i, True]["ttfb_p95_ms"] - cells[i, False]["ttfb_p95_ms"] for i in range(args.repeats)]
            ratios = [cells[i, True]["mib_per_second"] / cells[i, False]["mib_per_second"] for i in range(args.repeats)]
            report["summary"].append({"limit": limit, "p95_changes_ms": deltas,
                                      "median_change_ms": statistics.median(deltas),
                                      "throughput_ratios": ratios, "median_throughput_ratio": statistics.median(ratios)})
        report["completed"] = True
    finally:
        bind_threads(os.getpid(), cpus)
        report["verified_bytes"] = sum(r["bytes"] for r in rows)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
