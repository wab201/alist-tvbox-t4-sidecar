"""Paired fast-stream diagnostics; complete hashes, not a production admission gate."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil

from scripts.benchmark_rust_media import MemorySampler, cpu_seconds, percentile
from scripts.diagnose_rust_ttfb import TRACE_EVENTS, detailed_pairs, summary
from tests.rust.relay_support import MIB, PROFILE_HEADER, binary_path, experiment, pattern


async def batch(client, url, *, size, concurrency, prefix, rss, consumer, trace):
    samples = []
    expected = hashlib.sha256()
    for offset in range(0, size, 65536):
        expected.update(pattern(offset, min(65536, size - offset)))
    expected = expected.digest()
    transferred = hash_ns = 0

    async def consume(worker):
        nonlocal transferred, hash_ns
        digest = hashlib.sha256()
        row = {
            "trace": f"{prefix}-{worker}", "worker": worker, "request_number": 0,
            "transport_us": {},
        }
        start = time.monotonic()
        row["client_start_us"] = time.time_ns() // 1000
        headers = {"Range": f"bytes=0-{size - 1}"}
        if trace:
            headers[PROFILE_HEADER] = row["trace"]
        received = chunks = own_hash_ns = 0
        sizes = Counter()

        async def observed(name, info):
            if name in TRACE_EVENTS:
                row["transport_us"][name] = time.time_ns() // 1000

        worker_client = client[worker] if isinstance(client, list) else client
        async with worker_client.stream(
            "GET", url, headers=headers, extensions={"trace": observed} if trace else {},
        ) as response:
            row["client_headers_us"] = time.time_ns() // 1000
            address = response.extensions["network_stream"].get_extra_info("client_addr")
            row["client_port"] = address[1] if address else None
            assert response.status_code == 206, f"unexpected media status {response.status_code}"
            assert response.headers["content-length"] == str(size)
            async for chunk in response.aiter_raw():
                if chunks == 0:
                    row["client_first_body_us"] = time.time_ns() // 1000
                    row["ttfb_ms"] = (time.monotonic() - start) * 1000
                    row["other_bytes_before_first"] = transferred
                    row["other_hash_ms_before_first"] = hash_ns / 1e6
                received += len(chunk)
                transferred += len(chunk)
                chunks += 1
                sizes[len(chunk)] += 1
                began = time.perf_counter_ns()
                digest.update(chunk)
                hashed = time.perf_counter_ns() - began
                hash_ns += hashed
                own_hash_ns += hashed
                if consumer == "per-chunk-rss":
                    rss.sample()
                if consumer == "yield":
                    await asyncio.sleep(0)
        assert received == size and digest.digest() == expected
        row.update(bytes=received, chunks=chunks, chunk_sizes=dict(sizes),
                   seconds=time.monotonic() - start, hash_ms=own_hash_ns / 1e6)
        samples.append(row)

    started = time.monotonic()
    await asyncio.gather(*(consume(worker) for worker in range(concurrency)))
    elapsed = time.monotonic() - started
    return {
        "samples": samples, "seconds": elapsed, "bytes": size * concurrency,
        "mib_per_second": size * concurrency / MIB / elapsed,
        "hash_ms": hash_ns / 1e6,
        "ttfb_p95_ms": percentile([row["ttfb_ms"] for row in samples], .95),
        "hashes_match": True,
    }


async def measure(relays, *, mode, consumer, size, concurrency, batches, trace, prefix,
                  connections="shared"):
    process = psutil.Process(relays["rust"].process.pid)
    before_cpu = cpu_seconds(process)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    with MemorySampler(process, enabled=consumer != "per-chunk-rss") as rss:
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(httpx.AsyncClient(
                trust_env=False, timeout=30, limits=limits,
            ))
            workers = client
            if connections == "per-worker":
                workers = [await stack.enter_async_context(httpx.AsyncClient(
                    trust_env=False, timeout=30,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                )) for _ in range(concurrency)]
            before = (await client.get(relays["rust"].url + "/health")).json()
            cold = await batch(
                workers, relays["rust"].url + "/t4/media/hedge-fast",
                size=size, concurrency=concurrency, prefix=prefix + "-cold",
                rss=rss, consumer=consumer, trace=trace,
            )
            warm = []
            for index in range(batches):
                warm.append(await batch(
                    workers, relays["rust"].url + "/t4/media/hedge-fast",
                    size=size, concurrency=concurrency, prefix=f"{prefix}-b{index}",
                    rss=rss, consumer=consumer, trace=trace,
                ))
            # Health/profiling and client construction are outside transfer timing.
            deadline = time.monotonic() + 2
            while True:
                after = (await client.get(relays["rust"].url + "/health")).json()
                source = (await client.get(relays["origin"].url + "/health")).json()
                if not any((after["active"], after["hedge_active"], source["active"])):
                    break
                assert time.monotonic() < deadline
                await asyncio.sleep(.01)
            origins = (await client.get(relays["origin"].url + "/__profile")).json() if trace else {}
    samples = [sample for result in warm for sample in result["samples"]]
    paired = None
    if trace:
        events = [json.loads(line) for line in relays["rust"].log.read_text(
            encoding="utf-8",
        ).splitlines()]
        paired = detailed_pairs(samples, origins["events"], events)
        assert paired["matched"] == paired["expected"] == len(samples)
        assert paired["negative_clock_samples"] == 0
    assert after["hedge_started"] == before["hedge_started"] == 0, "fast isolation must not hedge"
    seconds = sum(row["seconds"] for row in warm)
    return {
        "mode": mode, "consumer": consumer, "connections": connections, "cold": cold, "warm": warm,
        "warm_samples": len(samples), "warm_ttfb": summary([row["ttfb_ms"] for row in samples]),
        "mib_per_second": sum(row["bytes"] for row in warm) / MIB / seconds,
        "transfer_seconds": seconds, "cpu_seconds": cpu_seconds(process) - before_cpu,
        "rss_peak": rss.peak, "rss_calls": rss.calls, "rss_query_ms": rss.elapsed_ns / 1e6,
        "consumer_connection_count": len({row["client_port"] for row in samples
                                          if row["client_port"] is not None}),
        "paired": paired, "extra_requests": 0,
        "bytes": sum(row["bytes"] for row in [cold, *warm]), "hashes_match": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--batches", type=int, default=8)
    parser.add_argument("--object-mib", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--consumer", nargs="+", choices=["per-chunk-rss", "sampled", "yield"],
                        default=["per-chunk-rss", "sampled"])
    parser.add_argument("--modes", nargs="+", choices=["single", "prefetch", "hedge"],
                        default=["single", "prefetch", "hedge"])
    parser.add_argument("--fair-source", action="store_true")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--connections", choices=["shared", "per-worker"], default="shared")
    args = parser.parse_args()
    if min(args.repeats, args.batches, args.object_mib, args.concurrency) < 1:
        parser.error("positive sizes required")
    rows = []
    allowed = psutil.Process().cpu_affinity()
    try:
        for repeat in range(args.repeats):
            cells = [(consumer, mode) for consumer in args.consumer for mode in args.modes]
            # Reverse whole cells on alternate rounds to counter order/thermal drift.
            if repeat % 2:
                cells.reverse()
            for consumer, mode in cells:
                psutil.Process().cpu_affinity(allowed)
                with experiment(
                    hedge_delay_ms={"single": None, "prefetch": 10000, "hedge": 150}[mode],
                    trace=args.trace, profile=args.trace, fair_source=args.fair_source,
                ) as relays:
                    for child in ("python", "rust", "origin"):
                        psutil.Process(relays[child].process.pid).cpu_affinity(
                            [allowed[0 if child == "origin" else -1]],
                        )
                    if len(allowed) > 2:
                        psutil.Process().cpu_affinity(allowed[1:-1])
                    result = asyncio.run(measure(
                        relays, mode=mode, consumer=consumer, size=args.object_mib * MIB,
                        concurrency=args.concurrency, batches=args.batches, trace=args.trace,
                        prefix=f"f-{repeat}-{consumer}-{mode}-{time.time_ns()}",
                        connections=args.connections,
                    ))
                    result.update(repeat=repeat, fair_source=args.fair_source)
                    rows.append(result)
                    print(json.dumps({key: result[key] for key in (
                        "repeat", "consumer", "mode", "warm_ttfb", "mib_per_second",
                        "rss_calls", "rss_query_ms",
                    )}), flush=True)
    finally:
        psutil.Process().cpu_affinity(allowed)
    report = {
        "scope": "fastpath-causal-diagnostics-not-admission", "production_ready": False,
        "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in ("scripts/diagnose_rust_fastpath.py",
                                       "tests/rust/relay_support.py",
                                       "rust/media-relay/src/main.rs",
                                       "rust/media-relay/src/hedge.rs")},
        "cpu_affinity": allowed, "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
