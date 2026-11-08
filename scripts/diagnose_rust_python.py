"""Same-origin Python/Rust node comparison; diagnostic, never production admission."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import platform
import statistics
import shutil
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil

from scripts.benchmark_rust_media import MemorySampler, cpu_seconds, memory
from scripts.diagnose_rust_fastpath import batch
from scripts.diagnose_rust_ttfb import detailed_pairs, summary
from tests.rust.relay_support import MIB, binary_path, experiment


def node_pairs(samples, origins, relays):
    result = detailed_pairs(samples, origins, relays)
    by_relay = {row.get("trace", row.get("client_trace")): row for row in relays}
    for row in result["samples"]:
        relay = by_relay[row["trace"]]
        points = row["points_us"]
        points.update(
            response_ready=relay.get("response_ready_us", relay.get("response_headers_us")),
            body_poll=relay.get("body_poll_us"),
            upstream_send_start=relay.get("transport_us", {}).get(
                "http11.send_request_headers.started",
            ),
            upstream_tcp_start=relay.get("transport_us", {}).get("connection.connect_tcp.started"),
            upstream_tcp_end=relay.get("transport_us", {}).get("connection.connect_tcp.complete"),
        )
        row["relay_body"] = {name: relay.get(name) for name in (
            "bytes", "chunks", "first_body_size", "max_chunk_size",
            "upstream_chunks", "max_upstream_chunk_size",
        )}
        for name, start, end in (
            ("headers_to_response", "relay_headers", "response_ready"),
            ("response_to_poll", "response_ready", "body_poll"),
            ("poll_to_first", "body_poll", "relay_first_body"),
            ("upstream_prepare_to_send", "upstream_request", "upstream_send_start"),
            ("upstream_tcp_connect", "upstream_tcp_start", "upstream_tcp_end"),
            ("upstream_send_to_origin_handler", "upstream_send_start", "origin_handler"),
        ):
            if points[start] is not None and points[end] is not None:
                row["detail_ms"][name] = (points[end] - points[start]) / 1000
    return result


def wire_pairs(row):
    """Only a response packet containing body bytes can bound first-body delivery."""
    result = []
    samples = {item["trace"]: item for item in row["samples"]}
    events = row["wire_events"]
    origin_port = row["wire_ports"]["origin"]
    relay_port = row["wire_ports"][row["mode"]]
    for item in row["paired"]["samples"]:
        sample = samples[item["trace"]]
        upstream = [event for event in events if event["kind"] == "request"
                    and event["target_port"] == origin_port and event["trace"] == item["trace"]]
        downstream = [event for event in events if event["kind"] == "response"
                      and event["source_port"] == relay_port
                      and event["target_port"] == sample["client_port"]
                      and event["timestamp_us"] >= sample["client_start_us"]]
        points = item["points_us"]
        pair = {"trace": item["trace"]}
        if len(upstream) == 1:
            stamp = upstream[0]["timestamp_us"]
            pair["upstream_send_to_kernel_ms"] = (stamp - points["upstream_request"]) / 1000
            pair["origin_kernel_to_handler_ms"] = (points["origin_handler"] - stamp) / 1000
        if len(downstream) == 1:
            stamp = downstream[0]["timestamp_us"]
            pair["downstream_body_with_headers"] = downstream[0]["body_with_headers"]
            pair["relay_first_to_response_kernel_ms"] = (stamp - points["relay_first_body"]) / 1000
            pair["response_kernel_to_client_first_ms"] = (points["client_first_body"] - stamp) / 1000
        result.append(pair)
    return result


async def compare(instances, *, size, concurrency, batches, repeats, connections, wire=False,
                  journal=None):
    rows = []
    origin_peers = {"python": set(), "rust": set()}
    allowed = psutil.Process().cpu_affinity()
    processes = {name: psutil.Process(instances[name].process.pid)
                 for name in ("origin", "python", "rust")}
    for name, process in processes.items():
        process.cpu_affinity([allowed[0 if name == "origin" else -1]])
    try:
        if len(allowed) > 2:
            psutil.Process().cpu_affinity(allowed[1:-1])
        async with AsyncExitStack() as stack:
            observer = await stack.enter_async_context(httpx.AsyncClient(
                trust_env=False, timeout=30,
            ))
            clients = {}
            for mode in ("python", "rust"):
                clients[mode] = [await stack.enter_async_context(httpx.AsyncClient(
                    trust_env=False, timeout=30,
                    limits=httpx.Limits(
                        max_connections=1 if connections == "per-worker" else concurrency,
                        max_keepalive_connections=1 if connections == "per-worker" else concurrency,
                    ),
                )) for _ in range(concurrency if connections == "per-worker" else 1)]
                if connections == "shared":
                    clients[mode] = clients[mode][0]
            for repeat in range(repeats):
                order = ("python", "rust") if repeat % 2 == 0 else ("rust", "python")
                for index in range(batches + 1):
                    for mode in order:
                        process = processes[mode]
                        before = {name: cpu_seconds(proc) for name, proc in processes.items()}
                        before["consumer"] = cpu_seconds(psutil.Process())
                        baseline = memory(process)
                        from tests.rust.wire_profile import WireProfile
                        capture = WireProfile({
                            urlsplit(instances[name].url).port for name in processes
                        }) if wire else nullcontext()
                        with MemorySampler(process, enabled=True) as rss:
                            with capture as captured:
                                row = await batch(
                                    clients[mode], instances[mode].url + "/t4/media/file",
                                    size=size, concurrency=concurrency,
                                    prefix=f"node-{mode}-{repeat}-{index}-{time.time_ns()}",
                                    rss=rss, consumer="sampled", trace=True,
                                )
                            used = {name: cpu_seconds(proc) - before[name]
                                    for name, proc in processes.items()}
                            used["consumer"] = cpu_seconds(psutil.Process()) - before["consumer"]
                        # Keep observation/client construction outside media timing.
                        deadline = time.monotonic() + 3
                        while True:
                            origin = (await observer.get(
                                instances["origin"].url + "/__profile",
                            )).json()["events"]
                            if mode == "rust":
                                relay = [json.loads(line) for line in instances[mode].log.read_text(
                                    encoding="utf-8",
                                ).splitlines()]
                            else:
                                relay = (await observer.get(
                                    instances[mode].url + "/__profile",
                                )).json()["events"]
                            paired = node_pairs(row["samples"], origin, relay)
                            if paired["matched"] == paired["expected"] == concurrency:
                                break
                            assert time.monotonic() < deadline, "incomplete node correlation"
                            await asyncio.sleep(.01)
                        assert paired["negative_clock_samples"] == 0
                        peers = {item["origin_peer_port"] for item in paired["samples"]}
                        new_peers = peers - origin_peers[mode]
                        origin_peers[mode].update(peers)
                        row.update(
                            mode=mode, repeat=repeat, batch=index,
                            phase="first" if repeat == index == 0 else (
                                "round-first" if index == 0 else "measured"
                            ),
                            paired=paired, cpu_seconds=used, rss_peak=rss.peak,
                            memory_before=baseline, memory_after=memory(process),
                            consumer_connection_count=len({
                                item["client_port"] for item in row["samples"]
                            }),
                            client_connections_opened=sum(
                                "connection.connect_tcp.started" in item["transport_us"]
                                for item in row["samples"]
                            ),
                            upstream_peer_ports_first_seen=len(new_peers),
                            wire_events=captured.events if wire else None,
                            wire_dropped=captured.dropped if wire else None,
                            wire_ports={name: urlsplit(instances[name].url).port for name in processes}
                            if wire else None,
                        )
                        row["wire_pairs"] = wire_pairs(row) if wire else None
                        rows.append(row)
                        if journal is not None:
                            with journal.open("a", encoding="utf-8") as output:
                                output.write(json.dumps(row) + "\n")
                        print(json.dumps({key: row[key] for key in (
                            "mode", "repeat", "batch", "phase", "mib_per_second", "ttfb_p95_ms",
                        )}), flush=True)
            health = (await observer.get(instances["rust"].url + "/health")).json()
            assert health["hedge_started"] == health["active"] == health["hedge_active"] == 0
    finally:
        psutil.Process().cpu_affinity(allowed)
    return rows


def comparisons(rows):
    result = []
    for repeat in sorted({row["repeat"] for row in rows}):
        group = {mode: [row for row in rows if row["repeat"] == repeat
                        and row["mode"] == mode and row["phase"] == "measured"]
                 for mode in ("python", "rust")}
        entry = {"repeat": repeat}
        for mode, samples in group.items():
            entry[mode] = {
                "ttfb": summary([sample["ttfb_ms"] for row in samples for sample in row["samples"]]),
                "mib_per_second": sum(row["bytes"] for row in samples) / MIB / sum(
                    row["seconds"] for row in samples
                ),
                "relay_cpu_seconds": sum(row["cpu_seconds"][mode] for row in samples),
                "rss_peak": max(row["rss_peak"] for row in samples),
            }
        entry["throughput_ratio"] = entry["rust"]["mib_per_second"] / entry["python"]["mib_per_second"]
        entry["ttfb_delta_ms"] = entry["rust"]["ttfb"]["p95"] - entry["python"]["ttfb"]["p95"]
        result.append(entry)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--object-mib", type=int, default=64)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--batches", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--connections", choices=["shared", "per-worker"], default="per-worker")
    parser.add_argument("--yield-after-first-body", action="store_true")
    parser.add_argument("--delivery-budget-kib", type=int)
    parser.add_argument("--wire-timing", action="store_true")
    args = parser.parse_args()
    if min(args.object_mib, args.concurrency, args.batches, args.repeats) < 1 or args.object_mib > 64:
        parser.error("positive counts and object-mib <=64 required")
    if args.delivery_budget_kib is not None and not 64 <= args.delivery_budget_kib <= 1024:
        parser.error("delivery budget must be 64..1024 KiB")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    journal = args.output.with_suffix(".rows.jsonl")
    if journal.exists() or args.output.exists():
        parser.error("preserve existing reports; choose a new output path")
    with experiment(
        profile=True, yield_after_first_body=args.yield_after_first_body,
        delivery_budget_kib=args.delivery_budget_kib,
    ) as instances:
        try:
            rows = asyncio.run(compare(
                instances, size=args.object_mib * MIB, concurrency=args.concurrency,
                batches=args.batches, repeats=args.repeats, connections=args.connections,
                wire=args.wire_timing, journal=journal,
            ))
        except BaseException:
            for name in ("origin", "python", "rust"):
                shutil.copy2(instances[name].log, args.output.with_suffix(f".{name}.log"))
            raise
    pairs = comparisons(rows)
    report = {
        "scope": "same-origin-python-rust-node-diagnostic", "production_ready": False,
        "platform": platform.platform(), "connections": args.connections,
        "yield_after_first_body": args.yield_after_first_body, "concurrency": args.concurrency,
        "delivery_budget_kib": args.delivery_budget_kib,
        "wire_timing": args.wire_timing,
        "object_mib": args.object_mib,
        "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
        "comparisons": pairs,
        "median_throughput_ratio": statistics.median(row["throughput_ratio"] for row in pairs),
        "median_ttfb_delta_ms": statistics.median(row["ttfb_delta_ms"] for row in pairs),
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "scripts/diagnose_rust_python.py", "tests/rust/relay_support.py",
            "rust/media-relay/src/main.rs", "rust/media-relay/src/hedge.rs",
            "sidecar/gateway/media_response.py", "sidecar/gateway/streaming.py", "sidecar/playback.py",
        )},
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "median_throughput_ratio", "median_ttfb_delta_ms",
    )}), flush=True)


if __name__ == "__main__":
    main()
