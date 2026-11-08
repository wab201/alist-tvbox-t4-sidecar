"""Bounded hedging comparison on synthetic assets; not a production release gate."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
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

from scripts.benchmark_rust_media import MemorySampler, cpu_seconds, percentile
from tests.rust.relay_support import MIB, binary_path, experiment, pattern


async def load(relays, scenario, *, repeat, concurrency=16, size=MIB, connections="per-worker"):
    timings = []
    scenario_number = ("fast", "slow-headers", "slow-body", "mixed",
                       "all-slow", "both-slow", "shared").index(scenario)
    offsets = [(scenario_number * 4 * MIB) + (repeat * 32 + worker) * 8192
               for worker in range(concurrency)]
    expected_hashes = []
    for offset in offsets:
        digest = hashlib.sha256()
        for position in range(offset, offset + size, 65536):
            digest.update(pattern(position, min(65536, offset + size - position)))
        expected_hashes.append(digest.digest())
    process = psutil.Process(relays["rust"].process.pid)
    before_cpu = cpu_seconds(process)
    baseline_rss = process.memory_info().rss
    with MemorySampler(process, enabled=True) as memory:
        result = await _load(
            relays, scenario, repeat=repeat, concurrency=concurrency, size=size,
            timings=timings, offsets=offsets, expected_hashes=expected_hashes,
            process=process, before_cpu=before_cpu, connections=connections,
        )
    result.update(rss_peak=memory.peak, rss_growth=memory.peak - baseline_rss,
                  rss_calls=memory.calls, rss_query_ms=memory.elapsed_ns / 1e6)
    return result


async def _load(relays, scenario, *, repeat, concurrency, size, timings, offsets,
                expected_hashes, process, before_cpu, connections):
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient(
            trust_env=False, timeout=5, limits=httpx.Limits(max_connections=concurrency),
        ))
        clients = [client] * concurrency
        if connections == "per-worker":
            clients = [await stack.enter_async_context(httpx.AsyncClient(
                trust_env=False, timeout=5,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            )) for _ in range(concurrency)]
        before = (await client.get(relays["rust"].url + "/health")).json()
        source_before = (await client.get(relays["origin"].url + "/health")).json()
        async def consume(worker):
            key = {
                "fast": "fast", "slow-headers": "headers", "slow-body": "body",
                "all-slow": "headers", "both-slow": "both-slow", "shared": "shared",
            }.get(scenario, "fast")
            if scenario == "mixed":
                key = "headers" if worker < 2 else "fast"
            offset = offsets[worker]
            digest, received = hashlib.sha256(), 0
            start = time.monotonic()
            first = True
            async with clients[worker].stream(
                "GET", relays["rust"].url + "/t4/media/hedge-" + key,
                headers={"Range": f"bytes={offset}-{offset + size - 1}"},
            ) as response:
                assert response.status_code == 206
                assert int(response.headers["content-length"]) == size
                async for chunk in response.aiter_raw():
                    if first:
                        timings.append((time.monotonic() - start) * 1000)
                        first = False
                    digest.update(chunk)
                    received += len(chunk)
            assert received == size and digest.digest() == expected_hashes[worker]
        # Measure only media transfer, excluding SSL/client setup and health I/O.
        started = time.monotonic()
        await asyncio.gather(*(consume(worker) for worker in range(concurrency)))
        elapsed = time.monotonic() - started
        await asyncio.sleep(.05)
        after = (await client.get(relays["rust"].url + "/health")).json()
        deadline = time.monotonic() + 2
        while True:
            source_after = (await client.get(relays["origin"].url + "/health")).json()
            if not any(source_after[key] for key in (
                "active", "hedge_header_active", "hedge_gate_active",
            )):
                break
            assert time.monotonic() < deadline, "loser still active"
            await asyncio.sleep(.01)
        after = (await client.get(relays["rust"].url + "/health")).json()
        assert after["active"] == after["hedge_active"] == 0
        assert after["hedge_started"] <= 2 + after["hedge_eligible"] // 8
    calls = sum(source_after["hedge_attempts"].values()) - sum(source_before["hedge_attempts"].values())
    return {
        "scenario": scenario, "repeat": repeat, "concurrency": concurrency,
        "ttfb_p50_ms": statistics.median(timings), "ttfb_p95_ms": percentile(timings, .95),
        "ttfb_sample_count": len(timings), "p95_sample_warning": len(timings) < 100,
        "ttfb_samples_ms": timings, "seconds": elapsed,
        "mib_per_second": concurrency * size / MIB / elapsed,
        "cpu_seconds": cpu_seconds(process) - before_cpu, "bytes": size * concurrency,
        "consumer_connections": connections, "timing_scope": "media-transfer-only",
        "source_requests": calls, "extra_requests": calls - concurrency,
        "source_generated_bytes": source_after["generated_bytes"] - source_before["generated_bytes"],
        "hedge_started": after["hedge_started"] - before["hedge_started"],
        "hedge_wins": after["hedge_wins"] - before["hedge_wins"],
        "hedge_skipped": after["hedge_skipped"] - before["hedge_skipped"],
        "hedge_cooldown_skipped": after["hedge_cooldown_skipped"] - before["hedge_cooldown_skipped"],
        "hedge_budget_skipped": after["hedge_budget_skipped"] - before["hedge_budget_skipped"],
        "hedge_eligible": after["hedge_eligible"] - before["hedge_eligible"],
        "hedge_credits_after": after["hedge_credits"],
        "hedge_peak": after["hedge_peak"], "hedge_active_after": after["hedge_active"],
        "hashes_match": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--hedge-delay-ms", type=int, default=150)
    parser.add_argument("--connections", choices=["shared", "per-worker"], default="per-worker")
    args = parser.parse_args()
    if args.repeats < 3 or args.hedge_delay_ms < 1:
        parser.error("at least three repeats and a positive hedge delay required")
    rows, affinities = [], []
    allowed = psutil.Process().cpu_affinity()
    try:
        for repeat in range(args.repeats):
            for enabled in ([False, True] if repeat % 2 == 0 else [True, False]):
                psutil.Process().cpu_affinity(allowed)
                with experiment(hedge_delay_ms=args.hedge_delay_ms if enabled else None,
                                trace=True) as relays:
                    for mode in ("python", "rust", "origin"):
                        psutil.Process(relays[mode].process.pid).cpu_affinity(
                            [allowed[0 if mode == "origin" else -1]],
                        )
                    if len(allowed) > 2:
                        psutil.Process().cpu_affinity(allowed[1:-1])
                    affinities.append({"repeat": repeat, "enabled": enabled, "cpus": allowed})
                    for scenario in ("fast", "slow-headers", "slow-body", "mixed",
                                     "all-slow", "both-slow", "shared"):
                        row = asyncio.run(load(
                            relays, scenario, repeat=repeat,
                            concurrency=1 if scenario in ("slow-headers", "slow-body") else 16,
                            connections=args.connections,
                        ))
                        row["enabled"] = enabled
                        rows.append(row)
                        print(json.dumps(row), flush=True)
    finally:
        psutil.Process().cpu_affinity(allowed)
    results = []
    for scenario in sorted({row["scenario"] for row in rows}):
        modes = {enabled: [row for row in rows if row["scenario"] == scenario and row["enabled"] == enabled]
                 for enabled in (False, True)}
        results.append({
            "scenario": scenario,
            "baseline_p95_ms": statistics.median(row["ttfb_p95_ms"] for row in modes[False]),
            "hedged_p95_ms": statistics.median(row["ttfb_p95_ms"] for row in modes[True]),
            "baseline_mib_per_second": statistics.median(row["mib_per_second"] for row in modes[False]),
            "hedged_mib_per_second": statistics.median(row["mib_per_second"] for row in modes[True]),
            "hedge_wins": sum(row["hedge_wins"] for row in modes[True]),
            "extra_requests": sum(row["extra_requests"] for row in modes[True]),
        })
    report = {
        "scope": "synthetic-hedging-efficacy-not-admission", "production_ready": False,
        "hedge_delay_ms": args.hedge_delay_ms, "hedge_limit": 2,
        "hedge_burst_credits": 2, "hedge_refill_per_eligible_requests": 8,
        "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "rust/media-relay/src/main.rs", "rust/media-relay/src/hedge.rs",
            "tests/rust/relay_support.py", "scripts/benchmark_rust_hedge.py",
            "scripts/benchmark_rust_media.py",
        )},
        "cpu_affinity": affinities, "comparisons": results, "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"comparisons": results}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
