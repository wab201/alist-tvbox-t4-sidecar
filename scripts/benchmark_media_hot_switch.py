"""Bounded same-origin audit of standalone Python versus the fixed hot-switch ingress."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil

from scripts.benchmark_rust_selected_media import ProcessConsumer
from scripts.benchmark_rust_media import cpu_seconds
from tests.rust.relay_support import Child, binary_path, pattern


@contextmanager
def gateway(directory, origin, *, standalone):
    directory.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "T4_PLUGIN_DIR": str(directory / "plugins"),
           "T4_LOG_FILE": str(directory / "events.jsonl"), "T4_MEDIA_ENGINE": "python",
           "ALIST_TVBOX_SUBSCRIPTION_URL": "", "T4_SOURCES_FILE": ""}
    with (directory / "runtime.log").open("wb") as log:
        process = subprocess.Popen([
            sys.executable, "-X", "utf8", "-m", "tests.rust.gateway_fixture",
            "--origin", origin, "--engine", "python", "--port", str(port),
            *(["--standalone-python"] if standalone else []),
        ], cwd=ROOT, env=env, stdout=log, stderr=log)
        parent = psutil.Process(process.pid)
        children = []
        try:
            with httpx.Client(trust_env=False, timeout=3) as client:
                base = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("test gateway exited")
                    try:
                        if client.get(base + "/t4/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.05)
                else:
                    raise RuntimeError("test gateway did not start")
                children = parent.children(recursive=True)
                key = client.get(base + "/fixture/keys").json()["file"]
                yield base, base + "/t4/media/" + key, [parent, *children], client
        finally:
            # A failed health check can still leave the relay child running.
            if parent.is_running():
                children = list({child.pid: child for child in [*children, *parent.children(recursive=True)]}.values())
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
            for child in children:
                try:
                    child.wait(3)
                except psutil.TimeoutExpired:
                    child.kill()
                    child.wait(3)
                except psutil.NoSuchProcess:
                    pass


async def audit(output: Path, rounds: int) -> dict:
    work = output.with_suffix(".work")
    work.mkdir(parents=True)
    rows = []
    size, concurrency = 1024 ** 2, 16
    expected = hashlib.sha256(b"".join(pattern(offset, 65536) for offset in range(0, size, 65536))).hexdigest()
    origin = Child([sys.executable, "-X", "utf8", "-m", "tests.rust.relay_support", "--serve", "origin"],
                   work, "origin")
    consumer = ProcessConsumer()
    try:
        await consumer.start()
        with gateway(work / "standalone", origin.url, standalone=True) as standalone, \
                gateway(work / "switchable", origin.url, standalone=False) as switchable:
            for repeat in range(rounds):
                modes = ("standalone_python", "hot_python", "hot_rust")
                # Each mode occupies each position once per three-round block.
                order = modes[repeat % 3:] + modes[:repeat % 3]
                for mode in order:
                    base, url, processes, client = standalone if mode == "standalone_python" else switchable
                    if mode != "standalone_python":
                        response = client.put(base + "/t4/api/diagnostics/media-engine",
                                              json={"engine": "rust" if mode == "hot_rust" else "python"})
                        response.raise_for_status()
                    await consumer.batch(url, concurrency, 8192, opaque_verified=True, complete_range=True)
                    latest = client.get(base + "/t4/api/diagnostics/events?limit=1").json()["events"]
                    after = latest[-1]["seq"] if latest else 0
                    cpu = sum(cpu_seconds(process) for process in processes)
                    batch = await consumer.batch(url, concurrency, size, opaque_verified=True, complete_range=True)
                    used_cpu = sum(cpu_seconds(process) for process in processes) - cpu
                    samples = batch["samples"]
                    assert len(samples) == concurrency and all(
                        sample.get("passed") and sample.get("sha256") == expected
                        and sample.get("range_metadata") == [0, size - 1, 5368709120] for sample in samples
                    ), "byte or Range contract failed"
                    deadline = time.monotonic() + 3
                    while True:
                        events = client.get(base + "/t4/api/diagnostics/events", params={"after": after, "limit": 2000}).json()["events"]
                        closed = [event for event in events if event["stage"] == "media.closed" and event["bytes"] == size]
                        if len(closed) == concurrency or time.monotonic() >= deadline:
                            break
                        await asyncio.sleep(.02)
                    assert len(closed) == concurrency, "incomplete server evidence"
                    rows.append({
                        "mode": mode, "repeat": repeat, "concurrency": concurrency,
                        "bytes": size * concurrency, "passed": True,
                        "ttfb_p95_ms": sorted(sample["ttfb_ms"] for sample in samples)[math.ceil(len(samples) * .95) - 1],
                        "mib_per_second": size * concurrency / 1048576 / batch["seconds"],
                        "gateway_cpu_seconds": used_cpu,
                        "gateway_process_rss_sum_after_bytes": sum(process.memory_info().rss for process in processes),
                        "samples": samples, "server_events": events,
                        "logging": client.get(base + "/t4/api/diagnostics/media-engine").json()["logging"],
                        "resources": client.get(base + "/t4/api/diagnostics/resources").json(),
                        "relay": None if mode == "standalone_python" else client.get(base + "/t4/api/diagnostics/relay").json(),
                    })
                    print(json.dumps({key: rows[-1][key] for key in ("mode", "repeat", "ttfb_p95_ms", "mib_per_second")}), flush=True)
            summary = []
            for repeat in range(rounds):
                paired = {row["mode"]: row for row in rows if row["repeat"] == repeat}
                for mode in ("hot_python", "hot_rust"):
                    before, after = paired["standalone_python"], paired[mode]
                    summary.append({"repeat": repeat, "mode": mode,
                        "ttfb_change_ms": after["ttfb_p95_ms"] - before["ttfb_p95_ms"],
                        "throughput_ratio": after["mib_per_second"] / before["mib_per_second"]})
            result = {"schema": 1, "scope": "loopback_same_code_ingress_cost_not_nas_or_real_cdn",
                      "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
                      "consumer": consumer.identity, "rows": rows, "pairs": summary,
                      "memory_scope": "sum_process_rss_not_cgroup_memory_or_peak",
                      "percentiles": "nearest_rank",
                      "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                                  "packages": {name: importlib.metadata.version(name) for name in
                                               ("httpx", "httpcore", "uvicorn", "fastapi", "anyio", "psutil", "sniffio")}},
                      "source_sha256": {str(path.relative_to(ROOT)).replace("\\", "/"):
                                        hashlib.sha256(path.read_bytes()).hexdigest()
                                        for path in sorted((ROOT / "sidecar").rglob("*.py"))},
                      "logs_enabled_all_modes": True, "historical_performance_gate_overridden": False}
            output.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
            return result
    finally:
        await consumer.close()
        origin.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if not 3 <= args.rounds <= 6 or not binary_path().is_file():
        parser.error("requires 3-6 rounds and the built release relay")
    if args.output.exists() or args.output.with_suffix(".work").exists():
        parser.error("preserve previous evidence")
    asyncio.run(audit(args.output, args.rounds))
