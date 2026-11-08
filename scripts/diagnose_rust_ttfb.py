"""Diagnostic interventions for synthetic TTFB; never a production admission gate."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil

from scripts.benchmark_rust_media import (
    cpu_seconds, expected_hash, pair_profiles, percentile, transfer_load,
)
from tests.rust.relay_support import MIB, binary_path, experiment

TRACE_EVENTS = {
    "connection.connect_tcp.started", "connection.connect_tcp.complete",
    "http11.send_request_headers.started", "http11.send_request_headers.complete",
    "http11.receive_response_headers.started", "http11.receive_response_headers.complete",
    "http11.receive_response_body.started",
}


def summary(values):
    return {
        "count": len(values),
        "median": statistics.median(values) if values else None,
        "p95": percentile(values, .95) if values else None,
        "max": max(values, default=None),
    }


async def diagnostic_load(url, *, concurrency, size, expected, policy, trace_prefix):
    """Keep complete streaming hashes even for fairness interventions."""
    all_first = asyncio.Event()
    first_count = 0
    samples, lag = [], []
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    epoch_us = time.time_ns() // 1000
    loop = asyncio.get_running_loop()
    epoch_loop = loop.time()
    completed = 0
    transferred = hash_total_ns = 0
    # call_later records timer dispatch delay, not OS/socket packet timestamps.
    def tick(due):
        now = loop.time()
        lag.append({
            "resumed_us": epoch_us + int((now - epoch_loop) * 1_000_000),
            "lag_ms": max(0, (now - due) * 1000),
        })
        if completed < concurrency:
            due = now + .002
            loop.call_later(.002, tick, due)

    timer = loop.call_later(.002, tick, loop.time() + .002)
    async with httpx.AsyncClient(trust_env=False, timeout=30, limits=limits) as client:
        async def consume(worker):
            nonlocal first_count, completed, transferred, hash_total_ns
            started = time.monotonic()
            sample = {
                "trace": f"{trace_prefix}-{worker}", "request_number": 0, "worker": worker,
                "client_start_us": time.time_ns() // 1000, "transport_us": {},
            }
            digest = hashlib.sha256()
            received = chunks = hash_ns = 0
            chunk_sizes = Counter()
            async def trace(name, info):
                if name in TRACE_EVENTS:
                    sample["transport_us"][name] = time.time_ns() // 1000

            async with client.stream(
                "GET", url,
                headers={"Range": f"bytes=0-{size - 1}",
                         "x-t4-relay-client-trace": sample["trace"]},
                extensions={"trace": trace},
            ) as response:
                sample["client_headers_us"] = time.time_ns() // 1000
                assert response.status_code == 206
                assert response.headers["content-length"] == str(size)
                async for chunk in response.aiter_raw():
                    if chunks == 0:
                        sample["client_first_body_us"] = time.time_ns() // 1000
                        sample["ttfb_ms"] = (time.monotonic() - started) * 1000
                        sample["other_bytes_before_first"] = transferred
                        sample["other_hash_ms_before_first"] = hash_total_ns / 1e6
                        first_count += 1
                        if first_count == concurrency:
                            all_first.set()
                        if policy == "first-barrier":
                            await asyncio.wait_for(all_first.wait(), 5)
                    received += len(chunk)
                    transferred += len(chunk)
                    chunks += 1
                    chunk_sizes[len(chunk)] += 1
                    before = time.perf_counter_ns()
                    digest.update(chunk)
                    hashed = time.perf_counter_ns() - before
                    hash_ns += hashed
                    hash_total_ns += hashed
                    if policy == "yield":
                        await asyncio.sleep(0)
            assert received == size and digest.hexdigest() == expected
            sample.update({
                "bytes": received, "chunks": chunks, "hash_ms": hash_ns / 1e6,
                "chunk_sizes": dict(chunk_sizes),
                "seconds": time.monotonic() - started,
            })
            samples.append(sample)
            completed += 1

        try:
            await asyncio.gather(*(consume(worker) for worker in range(concurrency)))
        finally:
            completed = concurrency
            timer.cancel()
    return samples, lag


def detailed_pairs(samples, origins, relays, *, direct=False):
    paired = pair_profiles(samples, origins, relays, direct=direct)
    by_origin = {row["trace"]: row for row in origins}
    by_relay = {row.get("trace", row.get("client_trace")): row for row in relays}
    by_sample = {row["trace"]: row for row in samples}
    for row in paired["samples"]:
        sample = by_sample[row["trace"]]
        origin = by_origin[row["trace"]]
        relay = origin if direct else by_relay[row["trace"]]
        # Raw points are retained: unlike percentiles, a single request is additive.
        points = {
            "client_start": sample["client_start_us"],
            "client_send_start": sample["transport_us"].get("http11.send_request_headers.started"),
            "client_send_end": sample["transport_us"].get("http11.send_request_headers.complete"),
            "relay_handler": relay["handler_us"],
            "upstream_request": relay.get("upstream_request_us"),
            "origin_handler": origin["handler_us"],
            "origin_headers": origin.get("response_headers_us"),
            "origin_first_body": origin.get("first_body_us"),
            "origin_first_body_sent": origin.get("first_body_sent_us"),
            "relay_headers": relay.get("upstream_headers_us"),
            "relay_first_body": relay.get("first_body_us"),
            "client_headers": sample["client_headers_us"],
            "client_first_body": sample["client_first_body_us"],
        }
        row["points_us"] = points
        row["worker"] = sample["worker"]
        row["chunks"] = sample["chunks"]
        row["hash_ms"] = sample["hash_ms"]
        spans = [
            ("client_pre_send", "client_start", "client_send_start"),
            ("client_send", "client_send_start", "client_send_end"),
            ("send_to_handler", "client_send_end", "relay_handler"),
            ("origin_headers_to_body", "origin_headers", "origin_first_body"),
            ("origin_first_send", "origin_first_body", "origin_first_body_sent"),
            ("origin_first_to_relay_first", "origin_first_body", "relay_first_body"),
            ("client_header_to_body", "client_headers", "client_first_body"),
        ]
        row["detail_ms"] = {
            name: (points[end] - points[start]) / 1000 for name, start, end in spans
            if points[start] is not None and points[end] is not None
        }
    return paired


def measure(instances, mode, *, concurrency, size, expected, policy, repeat, fair, wire=False):
    child = instances[mode]
    procs = {
        "consumer": psutil.Process(),
        "origin": psutil.Process(instances["origin"].process.pid),
        "relay": psutil.Process(child.process.pid),
    }
    before = {name: cpu_seconds(proc) for name, proc in procs.items()}
    offset = instances["rust"].log.stat().st_size
    started = time.monotonic()
    from contextlib import nullcontext
    from tests.rust.wire_profile import WireProfile
    capture = WireProfile({urlsplit(row.url).port for name, row in instances.items()
                           if name != "directory"}) if wire else nullcontext()
    with capture as captured:
        samples, lag = asyncio.run(diagnostic_load(
            child.url + ("/file" if mode == "origin" else "/t4/media/file"),
            concurrency=concurrency, size=size, expected=expected, policy=policy,
            trace_prefix=f"d-{mode}-{time.time_ns()}",
        ))
    elapsed = time.monotonic() - started
    used = {name: cpu_seconds(proc) - before[name] for name, proc in procs.items()}
    with httpx.Client(trust_env=False, timeout=5) as client:
        origins = client.get(instances["origin"].url + "/__profile").json()
        if mode == "rust":
            deadline = time.monotonic() + 2
            while True:
                with instances["rust"].log.open("rb") as log:
                    log.seek(offset)
                    events = [json.loads(line) for line in log if line.endswith(b"\n")]
                events = [row for row in events if row.get("event") == "relay.closed"]
                traces = {row.get("client_trace") for row in events}
                if all(row["trace"] in traces for row in samples) or time.monotonic() >= deadline:
                    break
                time.sleep(.01)
        else:
            events = origins["events"] if mode == "origin" else client.get(
                child.url + "/__profile",
            ).json()["events"]
    paired = detailed_pairs(samples, origins["events"], events, direct=mode == "origin")
    assert paired["matched"] == paired["expected"] == concurrency
    startup_end = max(row["client_first_body_us"] for row in samples)
    startup_lag = [row["lag_ms"] for row in lag if row["resumed_us"] <= startup_end + 2000]
    result = {
        "mode": mode, "repeat": repeat, "policy": policy, "fair_source": fair,
        "concurrency": concurrency, "size": size, "seconds": elapsed,
        "mib_per_second": concurrency * size / MIB / elapsed,
        "ttfb": summary([row["ttfb_ms"] for row in samples]),
        "client_loop_lag_ms": summary([row["lag_ms"] for row in lag]),
        "startup_loop_lag_ms": summary(startup_lag), "cpu_seconds": used,
        "hash_ms": sum(row["hash_ms"] for row in samples),
        "chunks": sum(row["chunks"] for row in samples),
        "hashes_match": True, "paired_profile": paired, "client_samples": samples,
        "wire_events": captured.events if wire else None,
        "wire_dropped": captured.dropped if wire else None,
        "wire_ports": {name: urlsplit(row.url).port for name, row in instances.items()
                       if name != "directory"} if wire else None,
    }
    print(json.dumps({key: result[key] for key in (
        "mode", "repeat", "policy", "fair_source", "ttfb", "mib_per_second", "hash_ms", "chunks",
    )}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--object-mib", type=int, default=64)
    parser.add_argument("--policy", nargs="+", choices=["original", "yield", "first-barrier"],
                        default=["original", "yield", "first-barrier"])
    parser.add_argument("--source", choices=["original", "fair", "both"], default="both")
    parser.add_argument("--wire-timing", action="store_true",
                        help="Linux isolated-loopback kernel timestamps, diagnostic overhead")
    args = parser.parse_args()
    if args.repeats < 1 or args.concurrency < 1 or not 1 <= args.object_mib <= 64:
        parser.error("positive repeats/concurrency and 1..64 MiB required")
    size = args.object_mib * MIB
    expected = expected_hash(size)
    original_affinity = psutil.Process().cpu_affinity()
    rows, affinities = [], []
    try:
        for fair in ([False, True] if args.source == "both" else [args.source == "fair"]):
            psutil.Process().cpu_affinity(original_affinity)
            with experiment(trace=True, profile=True, fair_source=fair) as instances:
                affinity = {}
                for mode in ("python", "rust", "origin"):
                    proc = psutil.Process(instances[mode].process.pid)
                    proc.cpu_affinity([original_affinity[0 if mode == "origin" else -1]])
                    affinity[mode] = proc.cpu_affinity()
                if len(original_affinity) > 2:
                    psutil.Process().cpu_affinity(original_affinity[1:-1])
                affinity["consumer"] = psutil.Process().cpu_affinity()
                affinities.append(affinity)
                for mode in ("python", "rust"):
                    asyncio.run(transfer_load(
                        instances[mode].url + "/t4/media/file", concurrency=8,
                        duration=.1, size=size, expected=expected,
                        trace_prefix=f"warm-{mode}-{time.time_ns()}",
                    ))
                for repeat in range(args.repeats):
                    policies = args.policy if repeat % 2 == 0 else list(reversed(args.policy))
                    for policy in policies:
                        modes = ("origin", "python", "rust") if repeat % 2 == 0 else (
                            "origin", "rust", "python",
                        )
                        for mode in modes:
                            rows.append(measure(
                                instances, mode, concurrency=args.concurrency, size=size,
                                expected=expected, policy=policy, repeat=repeat, fair=fair,
                                wire=args.wire_timing,
                            ))
    finally:
        psutil.Process().cpu_affinity(original_affinity)
    report = {
        "scope": "same-host-synthetic-causal-diagnostics-not-admission",
        "platform": platform.platform(), "production_ready": False,
        "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                "scripts/diagnose_rust_ttfb.py", "tests/rust/relay_support.py",
                "scripts/benchmark_rust_media.py", "rust/media-relay/src/main.rs",
            )
        },
        "cpu_affinity": affinities, "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
