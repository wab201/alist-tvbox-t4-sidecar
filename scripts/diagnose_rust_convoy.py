"""Attribute the original sustained 16-stream workload, without changing its consumer."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil

from scripts.benchmark_rust_media import expected_hash, measured_load, transfer_load
from tests.rust.relay_support import MIB, binary_path, experiment


def measured_downstream_load(instances, mode, **arguments):
    from tests.rust.wire_profile import ProcessFirstBodyWireProfile

    assert arguments.get("profile"), "downstream attribution requires request profiles"
    origin_port = urlsplit(instances["origin"].url).port
    body_port = urlsplit(instances[mode].url).port
    with ProcessFirstBodyWireProfile(
        {origin_port, body_port}, body_port=body_port, expected=arguments["concurrency"],
    ) as captured:
        row = measured_load(instances, mode, consumer_timing=True, **arguments)
    row["attribution"] = attribute(
        row, wire_events=captured.events, origin_port=origin_port, downstream=True,
        relay_port=body_port,
    )
    row.update(wire_events=captured.events, wire_dropped=captured.dropped)
    return row


def attribute(row, *, wire_events=None, origin_port=None, downstream=False, relay_port=None):
    """Only continuous intervals of one request are additive."""
    assert relay_port is None or wire_events is not None, "relay kernel correlation requires capture"
    raw = row["profile_raw"]
    origins = {item["trace"]: item for item in raw["origins"]}
    relays = {item.get("trace", item.get("client_trace")): item for item in raw["relays"]}
    samples = []
    for client in raw["clients"]:
        origin, relay = origins[client["trace"]], relays[client["trace"]]
        points = [
            client["client_start_us"], relay["handler_us"], relay["upstream_request_us"],
            origin["handler_us"], origin["response_headers_us"], origin["first_body_us"],
            relay["first_body_us"], client["client_first_body_us"],
        ]
        names = [
            "client_to_handler", "relay_prepare", "dispatch_to_origin_handler",
            "origin_prepare_headers", "origin_headers_to_body", "origin_body_to_relay",
            "relay_body_to_client",
        ]
        if wire_events is not None:
            incoming = [
                item["timestamp_us"] for item in wire_events
                if item["kind"] == "request" and item["target_port"] == origin_port
                and item["trace"] == client["trace"]
            ]
            assert len(incoming) == 1, "missing or duplicate origin kernel request"
            points.insert(3, incoming[0])
            names[2:3] = ["dispatch_to_origin_kernel", "origin_kernel_to_handler"]
        if downstream:
            assert client["request_number"] == 0, "downstream capture covers first batch only"
            body = [
                item["timestamp_us"] for item in wire_events
                if item["kind"] == "first-body" and item["trace"] == client["trace"]
            ]
            assert len(body) == 1, "missing or duplicate downstream first body"
            points.insert(-1, body[0])
            names[-1:] = ["relay_to_downstream_kernel", "downstream_kernel_to_consumer"]
        if relay_port is not None:
            incoming = [
                item for item in wire_events
                if item["kind"] == "request" and item["target_port"] == relay_port
                and item["trace"] == client["trace"]
            ]
            assert len(incoming) == 1, "missing or duplicate relay kernel request"
            request, = incoming
            points.insert(1, request["timestamp_us"])
            names[:1] = ["client_to_relay_kernel", "relay_kernel_to_handler"]
            if downstream:
                # This load opens a fresh AsyncClient; all captured requests are first-batch.
                syn = [item["timestamp_us"] for item in wire_events
                       if item["kind"] == "syn" and item["target_port"] == relay_port
                       and item["source_port"] == request["source_port"]]
                ack = [item["timestamp_us"] for item in wire_events
                       if item["kind"] == "syn-ack" and item["source_port"] == relay_port
                       and item["target_port"] == request["source_port"]]
                assert len(syn) == len(ack) == 1, "missing or duplicate relay TCP handshake"
                points[1:1] = [syn[0], ack[0]]
                names[:1] = ["client_before_syn", "relay_tcp_handshake", "client_connected_to_get"]
        spans = {name: (end - start) / 1000
                 for name, start, end in zip(names, points, points[1:])}
        # Cross-process ordering is only meaningful inside this isolated host.
        assert all(value >= 0 for value in spans.values()), "invalid clock correlation"
        sum_ms = sum(spans.values())
        assert abs(sum_ms - client["ttfb_ms"]) < 1, "timeline does not close"
        samples.append({
            "trace": client["trace"], "ttfb_ms": client["ttfb_ms"],
            "request_number": client["request_number"], "spans_ms": spans,
            "closure_error_ms": sum_ms - client["ttfb_ms"],
            "origin_peer_port": origin.get("peer_port"),
            "relay_chunks": relay.get("chunks"),
            "relay_max_chunk": relay.get("max_chunk_size"),
            "points_us": dict(zip([
                "client_start",
                *(["client_kernel_syn", "relay_kernel_synack"]
                  if downstream and relay_port is not None else []),
                *(["relay_kernel_request"] if relay_port is not None else []),
                "relay_handler", "upstream_request",
                *(["origin_kernel_request"] if wire_events is not None else []),
                "origin_handler", "origin_headers", "origin_first_body",
                "relay_first_body", *(["downstream_kernel_body"] if downstream else []),
                "client_first_body",
            ], points)),
        })
        if downstream:
            samples[-1]["consumer_hash_overlap_ms"] = sum(
                max(0, min(interval["end_us"], client["client_first_body_us"])
                    - max(interval["start_us"], body[0]))
                for interval in row["startup_hash_intervals"]
                if interval["trace"] != client["trace"]
            ) / 1000
    assert len(samples) == row["requests"], "missing profiled requests"
    tail = sorted(samples, key=lambda item: item["ttfb_ms"])[
        min(len(samples) - 1, int((len(samples) - 1) * .95 + .5))
    ]
    return {
        "matched": len(samples), "samples": samples, "p95_request": tail,
        "mean_spans_ms": {
            key: statistics.mean(item["spans_ms"][key] for item in samples)
            for key in tail["spans_ms"]
        },
        "first_requests": [item for item in samples if item["request_number"] == 0],
        "subsequent_requests": [item for item in samples if item["request_number"] > 0],
    }


def summarize(rows):
    cells = []
    for fair in sorted({row["fair_source"] for row in rows}):
        modes = {mode: [row for row in rows if row["fair_source"] == fair and row["mode"] == mode]
                 for mode in ("python", "rust")}
        if not all(modes.values()):
            continue
        middle = lambda mode, key: statistics.median(row[key] for row in modes[mode])
        delta = middle("rust", "ttfb_p95_ms") - middle("python", "ttfb_p95_ms")
        limit = max(2, middle("python", "ttfb_p95_ms") * .1)
        cell = {
            "fair_source": fair, "rounds": len(modes["python"]),
            "python_p95_round_median_ms": middle("python", "ttfb_p95_ms"),
            "rust_p95_round_median_ms": middle("rust", "ttfb_p95_ms"),
            "ttfb_delta_ms": delta, "unchanged_limit_ms": limit,
            "latency_within_budget": delta <= limit,
            "throughput_ratio": middle("rust", "mib_per_second") / middle("python", "mib_per_second"),
            "cpu_reduction": 1 - middle("rust", "cpu_seconds_per_gib") / middle("python", "cpu_seconds_per_gib"),
            "rss_reduction": 1 - middle("rust", "rss_peak") / middle("python", "rss_peak"),
            "requests": {mode: sum(row["requests"] for row in modes[mode]) for mode in modes},
            "hashes_match": all(row["hashes_match"] for group in modes.values() for row in group),
        }
        if all("attribution" in row for group in modes.values() for row in group):
            cell["mean_node_ms"] = {
                mode: {
                    name: statistics.mean(
                        item["spans_ms"][name] for row in modes[mode]
                        for item in row["attribution"]["samples"]
                    ) for name in modes[mode][0]["attribution"]["mean_spans_ms"]
                } for mode in modes
            }
        cells.append(cell)
    return cells


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--duration", type=float, default=3)
    parser.add_argument("--unprofiled", action="store_true")
    parser.add_argument("--source", choices=("original", "fair", "both"), default="both")
    parser.add_argument("--wire-timing", action="store_true",
                        help="Linux loopback timestamps; attribution only, not a performance gate")
    parser.add_argument("--downstream-timing", action="store_true",
                        help="correlate first body TCP sequence and startup hash overlap")
    args = parser.parse_args()
    if args.repeats < 3 or args.duration < 3:
        parser.error("at least three rounds and three seconds required")
    if args.wire_timing and args.unprofiled:
        parser.error("kernel attribution requires profiled request correlation")
    if args.downstream_timing and not args.wire_timing:
        parser.error("downstream timing requires kernel correlation")
    journal = args.output.with_suffix(".rows.jsonl")
    if args.output.exists() or journal.exists():
        parser.error("preserve previous evidence")
    allowed = psutil.Process().cpu_affinity()
    if len(allowed) < 4:
        parser.error("four CPUs required for isolated source/relay/consumer")
    size = 64 * MIB
    expected = expected_hash(size)
    rows = []
    report = {
        "scope": "original-async16-sustained-causal-diagnostic",
        "production_ready": False, "profile": not args.unprofiled,
        "wire_timing": args.wire_timing, "source": args.source,
        "downstream_timing": args.downstream_timing,
        "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "scripts/diagnose_rust_convoy.py", "scripts/benchmark_rust_media.py",
                "tests/rust/relay_support.py", "rust/media-relay/src/main.rs",
                "rust/media-relay/src/hedge.rs", "tests/rust/wire_profile.py",
            )
        }, "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Alternate the source condition between rounds as well as relay order.
        for repeat in range(args.repeats):
            conditions = (False, True)
            if args.source != "both":
                conditions = (args.source == "fair",)
            cells = list(conditions)
            if repeat % 2:
                cells.reverse()
            for fair in cells:
                psutil.Process().cpu_affinity(allowed)
                with experiment(trace=True, profile=not args.unprofiled, fair_source=fair) as instances:
                    for mode in ("origin", "python", "rust"):
                        psutil.Process(instances[mode].process.pid).cpu_affinity([
                            allowed[0 if mode == "origin" else -1],
                        ])
                    psutil.Process().cpu_affinity(allowed[1:-1])
                    for mode in ("python", "rust"):
                        asyncio.run(transfer_load(
                            instances[mode].url + "/t4/media/file", concurrency=8,
                            duration=.1, size=size, expected=expected,
                            trace_prefix=f"warm-convoy-{mode}-{time.time_ns()}",
                        ))
                    for mode in (("python", "rust") if repeat % 2 == 0 else ("rust", "python")):
                        from tests.rust.wire_profile import WireProfile
                        origin_port = urlsplit(instances["origin"].url).port
                        arguments = dict(
                            concurrency=16, duration=args.duration, size=size,
                            expected=expected, consumer_mode="async",
                            profile=not args.unprofiled,
                        )
                        if args.downstream_timing:
                            row = measured_downstream_load(instances, mode, **arguments)
                        else:
                            capture = WireProfile({origin_port}) if args.wire_timing else nullcontext()
                            with capture as captured:
                                row = measured_load(instances, mode, **arguments)
                            if not args.unprofiled:
                                row["attribution"] = attribute(
                                    row, wire_events=captured.events if args.wire_timing else None,
                                    origin_port=origin_port,
                                )
                            if args.wire_timing:
                                row.update(wire_events=captured.events, wire_dropped=captured.dropped)
                        row.update(repeat=repeat, fair_source=fair)
                        rows.append(row)
                        with journal.open("a", encoding="utf-8") as output:
                            output.write(json.dumps(row) + "\n")
                        print(json.dumps({
                            key: row[key] for key in (
                                "repeat", "fair_source", "mode", "requests", "ttfb_p95_ms",
                                "mib_per_second", "cpu_seconds_per_gib",
                            )
                        }), flush=True)
                    with httpx.Client(trust_env=False, timeout=5) as client:
                        deadline = time.monotonic() + 3
                        while True:
                            source = client.get(instances["origin"].url + "/health").json()
                            rust = client.get(instances["rust"].url + "/health").json()
                            if source["active"] == rust["active"] == 0:
                                break
                            assert time.monotonic() < deadline, "active requests not reclaimed"
                            time.sleep(.01)
                        assert rust["hedge_started"] == rust["errors"] == 0
    except BaseException as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        psutil.Process().cpu_affinity(allowed)
        expected_rows = args.repeats * (4 if args.source == "both" else 2)
        report["audit_completed"] = "error" not in report and len(rows) == expected_rows
        if rows:
            report["comparisons"] = summarize(rows)
        report["verified_bytes"] = sum(row["bytes"] for row in rows)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "verified_gib": report["verified_bytes"] / 1024 ** 3,
        "comparisons": report.get("comparisons"),
    }), flush=True)


if __name__ == "__main__":
    main()
