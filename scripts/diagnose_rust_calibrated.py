"""Same-instance, launch-pinned paired relay audit with observed connection reuse."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil

from scripts.benchmark_rust_media import MemorySampler, cpu_seconds, expected_hash, percentile
from scripts.diagnose_rust_affinity import bind_threads, thread_masks, verify_masks
from tests.rust.relay_support import MIB, binary_path, experiment


def runtime_identity():
    return {"python": platform.python_version(), "packages": {
        name: importlib.metadata.version(name)
        for name in ("httpx", "httpcore", "anyio", "uvicorn", "psutil")
    }}


def validate_connections(samples, seen, *, phase, previous_ports=()):
    events = {event["trace"]: event for event in seen if event.get("trace")}
    previous = set(previous_ports)
    ports = []
    for sample in samples:
        event = events.get(sample["trace"])
        if event is None or not isinstance(event.get("peer_port"), int):
            raise ValueError("missing upstream connection evidence")
        port = event["peer_port"]
        ports.append(port)
        sample["origin_peer_port"] = port
        sample["confirmed_reused"] = port in previous
        if phase == "warm" and port not in previous:
            raise ValueError("warm request opened an unprimed upstream connection")
        if phase == "cold" and port in previous:
            raise ValueError("cold request reused an upstream connection")
    return sorted(set(ports))


async def wave(client, url, *, concurrency, size, expected, prefix):
    async def consume(worker):
        trace = f"{prefix}-{worker}"
        started = time.perf_counter()
        digest, received, ttfb = hashlib.sha256(), 0, None
        async with client.stream("GET", url, headers={
            "Range": f"bytes=0-{size - 1}", "x-t4-relay-client-trace": trace,
        }) as response:
            assert response.status_code == 206
            assert response.headers["content-length"] == str(size)
            async for chunk in response.aiter_raw():
                if ttfb is None:
                    ttfb = (time.perf_counter() - started) * 1000
                received += len(chunk)
                digest.update(chunk)
        assert received == size and digest.hexdigest() == expected
        return {"trace": trace, "ttfb_ms": ttfb, "bytes": received,
                "sha256": digest.hexdigest()}
    return await asyncio.gather(*(consume(worker) for worker in range(concurrency)))


def schedstat(pid):
    result = {}
    for path in Path(f"/proc/{pid}/task").iterdir():
        try:
            runtime, waiting, slices = map(int, (path / "schedstat").read_text().split())
            result[path.name] = {"run_ns": runtime, "wait_ns": waiting, "slices": slices}
        except FileNotFoundError:
            continue
    return result


def sched_delta(before, after):
    return {tid: {key: value - before[tid][key] for key, value in row.items()}
            for tid, row in after.items() if tid in before}


def kernel_snapshot(cpus):
    result = {"cpu": {}, "cgroup": {}}
    for line in Path("/proc/stat").read_text().splitlines():
        parts = line.split()
        if parts and parts[0] in {f"cpu{cpu}" for cpu in cpus}:
            result["cpu"][parts[0]] = list(map(int, parts[1:]))
    for name in ("cpu.stat", "cpu.pressure", "memory.current", "memory.events"):
        path = Path("/sys/fs/cgroup") / name
        if path.is_file():
            result["cgroup"][name] = path.read_text().strip()
    return result


def summarize(rows):
    result = []
    for phase in sorted({row["phase"] for row in rows}):
        for concurrency in sorted({row["concurrency"] for row in rows}):
            pairs = {}
            for row in rows:
                if row["phase"] != phase or row["concurrency"] != concurrency:
                    continue
                assert row["hashes_match"] and row["connection_verified"]
                key = row["repeat"], row["mode"]
                assert key not in pairs, "duplicate observation"
                pairs[key] = row
            repeats = sorted({key[0] for key in pairs})
            assert len(repeats) >= 3 and len(pairs) == len(repeats) * 3
            changes, ratios = [], []
            for repeat in repeats:
                py, rust = pairs[repeat, "python"], pairs[repeat, "rust"]
                changes.append(rust["ttfb_p95_ms"] - py["ttfb_p95_ms"])
                ratios.append(rust["mib_per_second"] / py["mib_per_second"])
            result.append({"phase": phase, "concurrency": concurrency,
                           "paired_rust_minus_python_ms": changes,
                           "median_change_ms": statistics.median(changes),
                           "improved_rounds": sum(value < 0 for value in changes),
                           "median_throughput_ratio": statistics.median(ratios),
                           "small_tail_sample": concurrency < 100})
    return result


async def run_matrix(instances, args, record):
    size = args.object_mib * MIB
    expected = expected_hash(size)
    previous = {mode: set() for mode in ("origin", "python", "rust")}
    async with httpx.AsyncClient(trust_env=False, timeout=5) as admin:
        for concurrency in args.concurrency:
            for repeat in range(args.repeats):
                # Balanced Latin ordering across paired observations; never restart services.
                orders = (("origin", "python", "rust"), ("rust", "python", "origin"),
                          ("python", "origin", "rust"), ("rust", "origin", "python"))
                for mode in orders[repeat % len(orders)]:
                    child = instances[mode]
                    url = child.url + ("/file" if mode == "origin" else "/t4/media/file")
                    limits = httpx.Limits(max_connections=concurrency,
                                         max_keepalive_connections=concurrency,
                                         keepalive_expiry=5)
                    # Both upstream pools and the fixture server expire idle sockets at 5s.
                    await asyncio.sleep(args.cold_idle)
                    async with httpx.AsyncClient(trust_env=False, timeout=30, limits=limits) as client:
                        for phase in ("cold", "warm"):
                            verify_budget(instances, args.layout)
                            proc = psutil.Process(child.process.pid)
                            processes = {name: psutil.Process(instances[name].process.pid)
                                         for name in ("origin", "python", "rust")}
                            processes["consumer"] = psutil.Process()
                            before_cpu = {name: cpu_seconds(p) for name, p in processes.items()}
                            before_sched = {name: schedstat(p.pid) for name, p in processes.items()}
                            prefix = f"cal-{mode}-{concurrency}-{repeat}-{phase}-{time.time_ns()}"
                            with MemorySampler(proc, enabled=True) as sampler:
                                started = time.perf_counter()
                                samples = await wave(client, url, concurrency=concurrency,
                                                     size=size, expected=expected, prefix=prefix)
                                elapsed = time.perf_counter() - started
                            used = {name: cpu_seconds(p) - before_cpu[name]
                                    for name, p in processes.items()}
                            after_sched = {name: schedstat(p.pid) for name, p in processes.items()}
                            seen = (await admin.get(instances["origin"].url + "/health")).json()["seen"]
                            ports = validate_connections(samples, seen, phase=phase,
                                                         previous_ports=previous[mode])
                            previous[mode] = set(ports)
                            verify_budget(instances, args.layout)
                            record({"mode": mode, "phase": phase, "concurrency": concurrency,
                                    "repeat": repeat, "samples": samples, "requests": len(samples),
                                    "hashes_match": True, "connection_verified": True,
                                    "ttfb_p95_ms": percentile([v["ttfb_ms"] for v in samples], .95),
                                    "bytes": size * len(samples), "seconds": elapsed,
                                    "mib_per_second": size * len(samples) / MIB / elapsed,
                                    "cpu_seconds": used, "rss_peak": sampler.peak,
                                    "schedstat": {name: sched_delta(before_sched[name], after_sched[name])
                                                  for name in processes},
                                    "origin_ports": ports})


def verify_budget(instances, layout):
    for mode in ("origin", "python", "rust"):
        pid = instances[mode].process.pid
        verify_masks(thread_masks(pid), pid, layout[mode], [], "all-threads")
    verify_masks(thread_masks(os.getpid()), os.getpid(), layout["consumer"], [], "all-threads")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 4, 8, 16])
    parser.add_argument("--object-mib", type=int, default=64)
    parser.add_argument("--cold-idle", type=float, default=5.5)
    args = parser.parse_args()
    if sys.platform != "linux" or args.repeats < 3 or args.cold_idle <= 5:
        parser.error("Linux, >=3 rounds and idle >5s required")
    if args.object_mib < 1 or any(n < 1 or n > 32 for n in args.concurrency):
        parser.error("invalid bounded load")
    journal = args.output.with_suffix(".rows.jsonl")
    if args.output.exists() or journal.exists():
        parser.error("preserve existing evidence")
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 4:
        parser.error("four CPUs required")
    args.layout = {"origin": [allowed[0]], "python": [allowed[-1]],
                   "rust": [allowed[-1]], "consumer": allowed[1:-1]}
    rows = []
    report = {"scope": "calibrated-same-instance-cold-and-confirmed-reuse",
              "production_ready": False, "runtime": runtime_identity(), "cpu_layout": args.layout,
              "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
              "rows": rows, "legacy_gate_unchanged": True, "profile": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def record(row):
        rows.append(row)
        with journal.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row) + "\n")
        print(json.dumps({key: row[key] for key in (
            "mode", "phase", "concurrency", "repeat", "ttfb_p95_ms", "mib_per_second",
        )}), flush=True)

    try:
        with experiment(trace=True, connections=True, cpu_layout=args.layout) as instances:
            bind_threads(os.getpid(), args.layout["consumer"])
            report["pids"] = {mode: instances[mode].process.pid for mode in ("origin", "python", "rust")}
            asyncio.run(run_matrix(instances, args, record))
            report["summary"] = summarize(rows)
            with httpx.Client(trust_env=False, timeout=5) as client:
                health = client.get(instances["rust"].url + "/health").json()
                assert health["active"] == health["starting"] == health["errors"] == 0
                report["health"] = health
            report["completed"] = True
    except BaseException as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        bind_threads(os.getpid(), allowed)
        report["verified_bytes"] = sum(row["bytes"] for row in rows)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
