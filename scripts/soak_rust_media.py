"""Synthetic sustained transfers, pauses, seeks and cancellations, not device playback."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import statistics
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil

from tests.rust.relay_support import MIB, experiment, pattern
from scripts.benchmark_rust_media import cpu_seconds, memory


def expected_hash(offset, size):
    digest = hashlib.sha256()
    for position in range(offset, offset + size, 65536):
        digest.update(pattern(position, min(65536, offset + size - position)))
    return digest.hexdigest()


def soak(instances, mode, duration):
    process = psutil.Process(instances[mode].process.pid)
    before = process.memory_info().rss
    before_memory = memory(process)
    before_cpu = cpu_seconds(process)
    stop = threading.Event()
    started = time.monotonic()
    samples, latencies = [], []
    lock = threading.Lock()
    completed = total_bytes = 0
    base = instances[mode].url

    def consume(index):
        nonlocal completed, total_bytes
        size = 2 * MIB
        with httpx.Client(trust_env=False, timeout=5) as client:
            iteration = 0
            while not stop.is_set():
                offset = index * 12987 + iteration * 8191
                digest = hashlib.sha256()
                received = 0
                with client.stream("GET", base + "/t4/media/large", headers={
                    "Range": f"bytes={offset}-{offset + size - 1}",
                }) as response:
                    assert response.status_code == 206
                    assert response.headers["Content-Range"] == f"bytes {offset}-{offset + size - 1}/{5 * 1024 ** 3}"
                    for chunk in response.iter_raw(65536):
                        digest.update(chunk)
                        received += len(chunk)
                        time.sleep(.025)
                assert received == size and digest.hexdigest() == expected_hash(offset, size)
                with lock:
                    completed += 1
                    total_bytes += received
                iteration += 1
                if iteration % 5 == 0:
                    stop.wait(.5)

    def seek_and_cancel():
        with httpx.Client(trust_env=False, timeout=5) as client:
            index = 0
            while not stop.wait(.2):
                offset = (5 * 1024 ** 3 // 2) + index * 11939
                at = time.monotonic()
                response = client.get(base + "/t4/media/large", headers={
                    "Range": f"bytes={offset}-{offset + 8191}",
                })
                assert response.status_code == 206 and response.content == pattern(offset, 8192)
                assert client.get(base + "/health").status_code == 200
                latencies.append((time.monotonic() - at) * 1000)
                if index % 5 == 0:
                    with client.stream("GET", base + "/t4/media/slow") as response:
                        assert next(response.iter_raw(8192)) == pattern(0, 8192)
                index += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as workers:
        futures = [workers.submit(consume, index) for index in range(4)]
        futures.append(workers.submit(seek_and_cancel))
        try:
            while time.monotonic() - started < duration:
                for future in futures:
                    if future.done():
                        future.result()
                        raise AssertionError("soak worker stopped early")
                samples.append(process.memory_info().rss)
                stop.wait(.2)
        finally:
            stop.set()
        for future in futures:
            future.result(timeout=10)
    with httpx.Client(trust_env=False, timeout=5) as client:
        deadline = time.monotonic() + 5
        while True:
            origin = client.get(instances["origin"].url + "/health").json()
            relay = client.get(base + "/health").json()
            if origin["active"] == 0 and (mode != "rust" or relay["active"] == 0):
                break
            if time.monotonic() >= deadline:
                raise AssertionError("active upstream or relay after all consumers finished")
            time.sleep(.05)
    tail = statistics.median(samples[len(samples) * 3 // 4:])
    first = statistics.median(samples[:max(1, len(samples) // 4)])
    growth = tail - first
    assert growth < 16 * MIB, "sustained RSS grew by more than 16 MiB"
    assert len(latencies) >= duration, "seek workload did not stay active"
    return {
        "mode": mode, "duration_seconds": time.monotonic() - started,
        "completed_objects": completed, "verified_bytes": total_bytes,
        "hashes_match": True, "seek_checks": len(latencies),
        "seek_and_health_p95_ms": sorted(latencies)[int((len(latencies) - 1) * .95)],
        "rss_before": before, "rss_peak": max(samples),
        "rss_after": process.memory_info().rss, "rss_first_to_last_quarter_growth": growth,
        "memory_before": before_memory, "memory_after": memory(process),
        "relay_cpu_seconds": cpu_seconds(process) - before_cpu,
        "relay_health_after": relay,
        "active_after": origin["active"], "passed": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=180)
    parser.add_argument("--async-log", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "tests/reports/rust-media/local-soak.json")
    args = parser.parse_args()
    if args.duration < 3:
        parser.error("at least three seconds required")
    report = {
        "scope": "synthetic-pauses-seeks-cancel-soak", "production_ready": False,
        "requested_duration_seconds": args.duration, "rows": [], "passed": False,
        "async_log": args.async_log,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with experiment(relay_args=["--async-log"] if args.async_log else []) as instances:
            for mode in ("python", "rust"):
                row = soak(instances, mode, args.duration)
                report["rows"].append(row)
                print(json.dumps(row), flush=True)
        report["passed"] = True
    except Exception as error:
        report["failure_type"] = type(error).__name__
        raise
    finally:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
