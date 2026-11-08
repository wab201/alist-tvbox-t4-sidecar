"""Opt-in wake/repoll probe overhead, then same-request kernel attribution on Linux."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_rust_media import expected_hash, measured_load
from scripts.diagnose_rust_affinity import bind_threads
from scripts.diagnose_rust_calibrated import kernel_snapshot, runtime_identity, schedstat, sched_delta, verify_budget
from scripts.diagnose_rust_convoy import measured_downstream_load
from tests.rust.relay_support import MIB, binary_path, experiment


def probe_effects(rows):
    results = []
    for concurrency in sorted({row["concurrency"] for row in rows if row["phase"] == "overhead"}):
        cells = {(row["repeat"], row["probe"]): row for row in rows
                 if row["phase"] == "overhead" and row["concurrency"] == concurrency}
        repeats = sorted({key[0] for key in cells})
        assert len(repeats) >= 3 and len(cells) == len(repeats) * 2
        changes, ratios = [], []
        for repeat in repeats:
            off, on = cells[repeat, False], cells[repeat, True]
            assert off["hashes_match"] and on["hashes_match"]
            changes.append(on["ttfb_p95_ms"] - off["ttfb_p95_ms"])
            ratios.append(on["mib_per_second"] / off["mib_per_second"])
        results.append({"concurrency": concurrency, "p95_changes_ms": changes,
                        "median_p95_change_ms": statistics.median(changes),
                        "throughput_ratios": ratios, "median_throughput_ratio": statistics.median(ratios)})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    journal = args.output.with_suffix(".rows.jsonl")
    if sys.platform != "linux" or args.repeats < 3:
        parser.error("Linux and >=3 rounds required")
    if args.output.exists() or journal.exists():
        parser.error("preserve prior evidence")
    cpus = sorted(os.sched_getaffinity(0))
    if len(cpus) < 4:
        parser.error("four CPUs required")
    layout = {"origin": [cpus[0]], "python": [cpus[-1]], "rust": [cpus[-1]], "consumer": cpus[1:-1]}
    rows = []
    report = {"scope": "startup-wake-repoll-and-kernel-attribution", "production_ready": False,
              "runtime": runtime_identity(), "cpu_layout": layout, "rows": rows,
              "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
              "probe_boundary": "future-wake-to-repoll-includes-parent-work-not-pure-scheduler-latency"}
    size, expected = 64 * MIB, expected_hash(64 * MIB)

    def measure(instances, mode, concurrency, repeat, phase, probe):
        verify_budget(instances, layout)
        pids = {name: instances[name].process.pid for name in ("origin", "python", "rust")}
        pids["consumer"] = os.getpid()
        before = {name: schedstat(pid) for name, pid in pids.items()}
        kernel_before = kernel_snapshot(cpus)
        with httpx.Client(trust_env=False, timeout=5) as client:
            runtime_before = client.get(instances["rust"].url + "/health").json()["runtime_observation"]
        function = measured_downstream_load if phase == "attribution" else measured_load
        row = function(instances, mode, concurrency=concurrency, duration=3, size=size,
                       expected=expected, consumer_mode="async", profile=phase == "attribution",
                       probe_startup=probe)
        row.update(phase=phase, probe=probe, repeat=repeat,
                   kernel_before=kernel_before, kernel_after=kernel_snapshot(cpus),
                   schedstat={name: sched_delta(before[name], schedstat(pid)) for name, pid in pids.items()})
        with httpx.Client(trust_env=False, timeout=5) as client:
            row["runtime_before"] = runtime_before
            row["runtime_after"] = client.get(instances["rust"].url + "/health").json()["runtime_observation"]
        verify_budget(instances, layout)
        rows.append(row)
        with journal.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row) + "\n")
        print(json.dumps({key: row[key] for key in (
            "phase", "probe", "mode", "concurrency", "repeat", "ttfb_p95_ms", "mib_per_second",
        )}), flush=True)

    try:
        for profile in (False, True):
            bind_threads(os.getpid(), cpus)
            with experiment(trace=True, profile=profile, cpu_layout=layout,
                            relay_args=("--observe-startup",)) as instances:
                bind_threads(os.getpid(), layout["consumer"])
                for concurrency in ([16] if profile else [1, 4, 8, 16]):
                    for repeat in range(args.repeats):
                        if profile:
                            for mode in (("python", "rust") if repeat % 2 == 0 else ("rust", "python")):
                                time.sleep(5.5)
                                measure(instances, mode, concurrency, repeat, "attribution", mode == "rust")
                        else:
                            for probe in ((False, True) if repeat % 2 == 0 else (True, False)):
                                time.sleep(5.5)
                                measure(instances, "rust", concurrency, repeat, "overhead", probe)
        report["probe_effects"] = probe_effects(rows)
        report["completed"] = True
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
