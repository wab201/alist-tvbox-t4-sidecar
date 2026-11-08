"""Local-only raw media transport gate. No AT, NAS or external media is contacted."""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil

from tests.rust.relay_support import MIB, binary_path, experiment, pattern


def percentile(values, fraction):
    return sorted(values)[min(len(values) - 1, int((len(values) - 1) * fraction + .5))]


def cpu_seconds(process):
    value = process.cpu_times()
    return value.user + value.system


class MemorySampler:
    """Bound resource observation independently of media chunk frequency."""

    def __init__(self, process, *, enabled):
        self.process = process
        self.peak = process.memory_info().rss
        self.calls = 0
        self.elapsed_ns = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True) if enabled else None

    def sample(self):
        started = time.perf_counter_ns()
        self.peak = max(self.peak, self.process.memory_info().rss)
        self.elapsed_ns += time.perf_counter_ns() - started
        self.calls += 1

    def run(self):
        while not self.stop.wait(.02):
            self.sample()

    def __enter__(self):
        if self.thread is not None:
            self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        self.sample()


def memory(process):
    value = process.memory_info()
    try:
        full = process.memory_full_info()
        unique = getattr(full, "uss", None)
        proportional = getattr(full, "pss", None)
    except (psutil.AccessDenied, AttributeError):
        unique = proportional = None
    return {"rss": value.rss, "uss": unique, "pss": proportional}


def expected_hash(size):
    digest = hashlib.sha256()
    for offset in range(0, size, 65536):
        digest.update(pattern(offset, min(65536, size - offset)))
    return digest.hexdigest()


async def transfer_load(url, *, concurrency, duration, size, expected, trace_prefix,
                        min_requests=1, consumer_timing=False, hash_offload=False,
                        probe_startup=False, admission_bypass=False):
    if consumer_timing and hash_offload:
        raise ValueError("synchronous hash attribution cannot measure offloaded work")
    hash_slot = asyncio.Semaphore(1) if hash_offload else None
    count = 0
    total = 0
    startup = []
    trace_samples = []
    hash_intervals = []
    first_workers = set()
    expires = time.monotonic() + duration
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(trust_env=False, timeout=30, limits=limits) as client:
        async def consume(worker):
            nonlocal count, total
            first_request = True
            request_number = 0
            while first_request or request_number * concurrency < min_requests or time.monotonic() < expires:
                first_request = False
                started = time.monotonic()
                started_us = time.time_ns() // 1000
                digest = hashlib.sha256()
                received = 0
                first_chunk = True
                trace = f"{trace_prefix}-{worker}-{request_number}"
                headers = {
                    "Range": f"bytes=0-{size - 1}",
                    "x-t4-relay-client-trace": trace,
                }
                if probe_startup:
                    headers["x-t4-relay-observe"] = "1"
                if admission_bypass:
                    headers["x-t4-relay-admission-bypass"] = "1"
                async with client.stream("GET", url, headers=headers) as response:
                    headers_us = time.time_ns() // 1000
                    assert response.status_code == 206, f"unexpected media status {response.status_code}"
                    assert response.headers["Content-Length"] == str(size)
                    async for chunk in response.aiter_raw():
                        if first_chunk:
                            ttfb_ms = (time.monotonic() - started) * 1000
                            startup.append(ttfb_ms)
                            trace_samples.append({
                                "trace": trace, "ttfb_ms": ttfb_ms,
                                "client_start_us": started_us, "client_headers_us": headers_us,
                                "client_first_body_us": time.time_ns() // 1000,
                                "request_number": request_number,
                            })
                            first_chunk = False
                            if consumer_timing:
                                first_workers.add(worker)
                        received += len(chunk)
                        if consumer_timing and len(first_workers) < concurrency:
                            hash_started = time.time_ns() // 1000
                            digest.update(chunk)
                            if len(hash_intervals) >= 100000:
                                raise RuntimeError("startup hash observation bound exceeded")
                            hash_intervals.append({
                                "start_us": hash_started, "end_us": time.time_ns() // 1000,
                                "trace": trace,
                            })
                        else:
                            if hash_offload:
                                # Diagnostic only: one awaited chunk per worker, never a
                                # detached/unbounded hash queue or a changed byte contract.
                                async with hash_slot:
                                    await asyncio.to_thread(digest.update, chunk)
                            else:
                                digest.update(chunk)
                assert received == size, "media length changed"
                assert digest.hexdigest() == expected, "media bytes changed"
                count += 1
                total += received
                request_number += 1

        await asyncio.gather(*(consume(worker) for worker in range(concurrency)))
    result = {
        "requests": count, "bytes": total, "hashes_match": True,
        "ttfb_p95_ms": percentile(startup, .95),
        "trace_samples": trace_samples,
    }
    if consumer_timing:
        result["startup_hash_intervals"] = hash_intervals
    return result


def independent_load(url, *, concurrency, duration, size, expected, trace_prefix,
                     min_requests=1):
    barrier = threading.Barrier(concurrency)

    def consume(worker):
        total, count, startup = 0, 0, []
        trace_samples = []
        with httpx.Client(trust_env=False, timeout=30) as client:
            barrier.wait(timeout=30)
            expires = time.monotonic() + duration
            while count == 0 or count * concurrency < min_requests or time.monotonic() < expires:
                digest, received = hashlib.sha256(), 0
                started = time.monotonic()
                started_us = time.time_ns() // 1000
                first_chunk = True
                trace = f"{trace_prefix}-{worker}-{count}"
                headers = {
                    "Range": f"bytes=0-{size - 1}",
                    "x-t4-relay-client-trace": trace,
                }
                with client.stream("GET", url, headers=headers) as response:
                    headers_us = time.time_ns() // 1000
                    assert response.status_code == 206, f"unexpected media status {response.status_code}"
                    assert response.headers["Content-Length"] == str(size)
                    for chunk in response.iter_raw():
                        if first_chunk:
                            ttfb_ms = (time.monotonic() - started) * 1000
                            startup.append(ttfb_ms)
                            trace_samples.append({
                                "trace": trace, "ttfb_ms": ttfb_ms,
                                "client_start_us": started_us, "client_headers_us": headers_us,
                                "client_first_body_us": time.time_ns() // 1000,
                                "request_number": count,
                            })
                            first_chunk = False
                        received += len(chunk)
                        digest.update(chunk)
                assert received == size and digest.hexdigest() == expected, "media bytes changed"
                total += received
                count += 1
        return total, count, startup, trace_samples

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as clients:
        results = list(clients.map(consume, range(concurrency)))
    return {
        "requests": sum(row[1] for row in results),
        "bytes": sum(row[0] for row in results), "hashes_match": True,
        "ttfb_p95_ms": percentile([value for row in results for value in row[2]], .95),
        "trace_samples": [sample for row in results for sample in row[3]],
    }


def latency_summary(samples):
    return {
        "count": len(samples),
        "p95_ms": percentile([sample["ttfb_ms"] for sample in samples], .95) if samples else None,
        "p95_sample_warning": len(samples) < 100,
    }


def pair_profiles(samples, origin_events, relay_events, *, direct=False):
    """Join the same request, using wall clocks only inside one host."""
    origins = {event["trace"]: event for event in origin_events}
    relays = {event.get("trace", event.get("client_trace")): event for event in relay_events}
    matched = []
    for sample in samples:
        origin = origins.get(sample["trace"])
        relay = origin if direct else relays.get(sample["trace"])
        if origin is None or relay is None:
            continue
        points = {
            "client_start": sample["client_start_us"],
            "relay_handler": relay["handler_us"],
            "upstream_request": relay.get("upstream_request_us"),
            "origin_handler": origin["handler_us"],
            "origin_headers": origin.get("response_headers_us"),
            "upstream_headers": relay.get("upstream_headers_us"),
            "relay_first_body": relay.get("first_body_us"),
            "client_headers": sample["client_headers_us"],
            "client_first_body": sample["client_first_body_us"],
        }
        stages = [
            ("before_handler", "client_start", "relay_handler"),
            ("relay_setup", "relay_handler", "upstream_request"),
            ("upstream_dispatch", "upstream_request", "origin_handler"),
            ("origin_prepare", "origin_handler", "origin_headers"),
            ("upstream_headers_delivery", "origin_headers", "upstream_headers"),
            ("upstream_first_body", "upstream_headers", "relay_first_body"),
            ("downstream_delivery", "relay_first_body", "client_first_body"),
        ] if not direct else [
            ("before_handler", "client_start", "origin_handler"),
            ("origin_prepare", "origin_handler", "origin_headers"),
            ("downstream_delivery", "origin_headers", "client_first_body"),
        ]
        deltas = {
            name: (points[end] - points[start]) / 1000
            for name, start, end in stages
            if points[start] is not None and points[end] is not None
        }
        matched.append({
            "trace": sample["trace"], "ttfb_ms": sample["ttfb_ms"],
            "request_number": sample.get("request_number"),
            "stages_ms": deltas, "origin_peer_port": origin.get("peer_port"),
            "origin_chunks": origin.get("chunks"), "relay_chunks": relay.get("chunks"),
            "clock_order_valid": all(value >= 0 for value in deltas.values()),
        })
    return {
        "matched": len(matched), "expected": len(samples), "samples": matched,
        "negative_clock_samples": sum(not row["clock_order_valid"] for row in matched),
        "stage_p95_ms": {
            stage: percentile(values, .95)
            for stage in {key for row in matched for key in row["stages_ms"]}
            if (values := [row["stages_ms"][stage] for row in matched if stage in row["stages_ms"]])
        },
        "origin_connections": len({row["origin_peer_port"] for row in matched}),
    }


def measured_load(instances, mode, *, concurrency, duration, size, expected, consumer_mode,
                  profile=False, min_requests=1, consumer_timing=False, hash_offload=False,
                  probe_startup=False, admission_bypass=False, log_settle_timeout=1):
    if consumer_mode != "async" and hash_offload:
        raise ValueError("hash offload is an async-consumer diagnostic only")
    child = instances[mode]
    process = psutil.Process(child.process.pid)
    source = psutil.Process(instances["origin"].process.pid)
    consumer = psutil.Process()
    rss_samples = []
    stop = threading.Event()
    baseline_memory = memory(process)
    log_offset = child.log.stat().st_size

    def sample():
        while not stop.wait(.02):
            rss_samples.append(process.memory_info().rss)

    sampler = threading.Thread(target=sample, daemon=True)
    before = {name: cpu_seconds(proc) for name, proc in [
        ("relay", process), ("origin", source), ("consumer", consumer),
    ]}
    sampler.start()
    started = time.monotonic()
    started_us = time.time_ns() // 1000
    try:
        trace_prefix = f"{mode}-{concurrency}-{time.time_ns()}"
        arguments = dict(
            concurrency=concurrency, duration=duration, size=size, expected=expected,
            trace_prefix=trace_prefix,
            min_requests=min_requests,
        )
        if consumer_mode == "threads":
            path = "/file" if mode == "origin" else "/t4/media/file"
            result = independent_load(child.url + path, **arguments)
        else:
            path = "/file" if mode == "origin" else "/t4/media/file"
            result = asyncio.run(transfer_load(
                child.url + path, **arguments, consumer_timing=consumer_timing,
                hash_offload=hash_offload,
                probe_startup=probe_startup,
                admission_bypass=admission_bypass,
            ))
    finally:
        elapsed = time.monotonic() - started
        stop.set()
        sampler.join()
    used = {name: cpu_seconds(proc) - before[name] for name, proc in [
        ("relay", process), ("origin", source), ("consumer", consumer),
    ]}
    result.update({
        "mode": mode, "concurrency": concurrency, "seconds": elapsed,
        "mib_per_second": result["bytes"] / MIB / elapsed,
        "cpu_seconds_per_gib": used["relay"] / (result["bytes"] / 1024 ** 3),
        "relay_cpu_seconds": used["relay"],
        "origin_cpu_seconds": used["origin"], "consumer_cpu_seconds": used["consumer"],
        "memory_before": baseline_memory, "memory_after": memory(process),
        "rss_peak": max([baseline_memory["rss"], *rss_samples]),
        "latency_sampling": {
            "all": latency_summary(result["trace_samples"]),
            "first_per_worker": latency_summary([
                row for row in result["trace_samples"] if row["request_number"] == 0
            ]),
            "subsequent": latency_summary([
                row for row in result["trace_samples"] if row["request_number"] > 0
            ]),
        },
    })
    client_samples = result["trace_samples"]
    if mode == "rust":
        traces = {sample["trace"] for sample in result["trace_samples"]}
        deadline = time.monotonic() + log_settle_timeout
        events = []
        while time.monotonic() < deadline:
            with child.log.open("rb") as log:
                log.seek(log_offset)
                events = [
                    json.loads(line)
                    for line in log
                    if line.endswith(b"\n")
                ]
            events = [
                event for event in events
                if event.get("event") == "relay.closed"
                and event.get("client_trace") in traces
            ]
            if traces.issubset({event.get("client_trace") for event in events}):
                break
            time.sleep(.01)
        by_trace = {event["client_trace"]: event for event in events}
        if probe_startup:
            assert len(events) == len(traces), "missing observed close events"
            assert all("open_observation" in event and "body_observation" in event for event in events)
            result["startup_observations"] = [
                {key: event[key] for key in ("client_trace", "open_observation", "body_observation")}
                for event in events
            ]
        matched = []
        for sample in result["trace_samples"]:
            event = by_trace.get(sample["trace"])
            if event is not None:
                matched.append({
                    **sample,
                    "upstream_headers_ms": event.get("upstream_headers_ms"),
                    "first_body_ms": event.get("first_body_ms"),
                    "duration_ms": event.get("duration_ms"),
                    "status": event.get("status"),
                    "bytes": event.get("bytes"),
                })
        result["trace_samples"] = matched
        result["trace_match_count"] = len(matched)
        result["trace_expected_count"] = len(traces)
        result["client_minus_first_body_p95_ms"] = percentile(
            [
                sample["ttfb_ms"] - sample["first_body_ms"]
                for sample in matched
                if sample["first_body_ms"] is not None
            ],
            .95,
        ) if any(sample["first_body_ms"] is not None for sample in matched) else None
        result["server_stages_p95_ms"] = {
            field: percentile(values, .95)
            for field in ("upstream_headers_ms", "first_body_ms", "duration_ms")
            if (values := [
                sample[field] for sample in matched
                if sample.get(field) is not None
            ])
        }
    if profile:
        with httpx.Client(trust_env=False, timeout=5) as client:
            origin_profile = client.get(instances["origin"].url + "/__profile").json()
            origin_events = origin_profile["events"]
            relay_events = events if mode == "rust" else (
                client.get(child.url + "/__profile").json()["events"]
            )
        result["paired_profile"] = pair_profiles(
            client_samples, origin_events, relay_events, direct=mode == "origin",
        )
        lag = [
            row["lag_ms"] for row in origin_profile["loop_lag"]
            if started_us <= row["resumed_us"] <= time.time_ns() // 1000
        ]
        result["origin_loop_lag_p95_ms"] = percentile(lag, .95) if lag else None
        result["origin_loop_lag_max_ms"] = max(lag, default=None)
        traces = {sample["trace"] for sample in client_samples}
        result["profile_raw"] = {
            "clients": client_samples,
            "origins": [row for row in origin_events if row["trace"] in traces],
            "relays": [row for row in relay_events
                       if row.get("trace", row.get("client_trace")) in traces],
        }
    if mode != "rust":
        result.pop("trace_samples", None)
    return result


def gate(rows, *, contracts_passed, repeats):
    groups = {}
    for row in rows:
        groups.setdefault(row["concurrency"], {}).setdefault(row["mode"], []).append(row)
    comparisons = []
    reasons = []
    if not contracts_passed:
        reasons.append("compatibility_not_passed")
    if repeats < 3:
        reasons.append("at_least_three_repetitions_required")
    if not {1, 4, 8, 16}.issubset(groups):
        reasons.append("missing_required_concurrency")
    if any(row.get("hashes_match") is not True for row in rows):
        reasons.append("media_byte_verification_missing_or_failed")
    for concurrency, modes in sorted(groups.items()):
        if any(len(modes.get(mode, [])) < repeats for mode in ("python", "rust")):
            reasons.append(f"incomplete_samples_{concurrency}")
            continue
        def middle(mode, field):
            return statistics.median(row[field] for row in modes[mode])

        python_cpu = middle("python", "cpu_seconds_per_gib")
        rust_cpu = middle("rust", "cpu_seconds_per_gib")
        if python_cpu <= 0 or rust_cpu < 0:
            reasons.append(f"invalid_cpu_measurement_{concurrency}")
        cpu_reduction = 1 - rust_cpu / max(python_cpu, 1e-9)
        rss_reduction = 1 - middle("rust", "rss_peak") / middle("python", "rss_peak")
        throughput_ratio = middle("rust", "mib_per_second") / middle("python", "mib_per_second")
        ttfb_delta = middle("rust", "ttfb_p95_ms") - middle("python", "ttfb_p95_ms")
        ttfb_limit = max(2, middle("python", "ttfb_p95_ms") * .1)
        passed = throughput_ratio >= .95 and ttfb_delta <= ttfb_limit
        if concurrency >= 4:
            passed &= (
                rust_cpu <= python_cpu * .8
                and middle("rust", "rss_peak") <= middle("python", "rss_peak") * .85
            )
        if not passed:
            reasons.append(f"performance_gate_failed_{concurrency}")
        comparisons.append({
            "concurrency": concurrency, "passed": passed,
            "cpu_reduction": cpu_reduction, "rss_reduction": rss_reduction,
            "throughput_ratio": throughput_ratio, "ttfb_p95_delta_ms": ttfb_delta,
            "python_mib_per_second": middle("python", "mib_per_second"),
            "rust_mib_per_second": middle("rust", "mib_per_second"),
            "python_cpu_seconds_per_gib": middle("python", "cpu_seconds_per_gib"),
            "rust_cpu_seconds_per_gib": middle("rust", "cpu_seconds_per_gib"),
            "python_rss_peak": middle("python", "rss_peak"),
            "rust_rss_peak": middle("rust", "rss_peak"),
        })
    return {"passed": not reasons, "reasons": reasons, "comparisons": comparisons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "tests/reports/rust-media/local-benchmark.json")
    parser.add_argument("--duration", type=float, default=3)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8, 16])
    parser.add_argument("--object-mib", type=int, default=64)
    parser.add_argument("--consumer", choices=["async", "threads"], default="async")
    parser.add_argument("--profile", action="store_true",
                        help="join same-host client/relay/origin stages (diagnostic overhead)")
    parser.add_argument("--downstream-timing", action="store_true",
                        help="diagnostic kernel first-body attribution at 16 streams, after the original history")
    parser.add_argument("--fair-source", action="store_true",
                        help="diagnostic source yielding; never substitutes the original source gate")
    parser.add_argument("--min-requests", type=int, default=1,
                        help="minimum requests per mode/round, evenly distributed over workers")
    parser.add_argument("--legacy-source", action="store_true",
                        help="reproduce the original fixture only; unsafe cancellation behavior")
    parser.add_argument("--yield-after-first-body", action="store_true",
                        help="diagnose one cooperative yield after Rust's first body frame")
    parser.add_argument("--delivery-budget-kib", type=int,
                        help="diagnose bounded Rust body scheduling, 64..1024 KiB")
    parser.add_argument("--upstream-read-kib", type=int,
                        help="controlled upstream transport: 0 adaptive, 8..1024 actual read cap")
    parser.add_argument("--startup-fairness", action="store_true",
                        help="cooperate only while other requests are waiting for first body")
    parser.add_argument("--async-log", action="store_true")
    parser.add_argument("--prebind-affinity", action="store_true",
                        help="Linux: inherit the CPU budget before any child runtime threads start")
    args = parser.parse_args()
    if (
        args.duration <= 0 or args.repeats < 1 or not 1 <= args.object_mib <= 64
        or any(value < 1 for value in args.concurrency) or args.min_requests < 1
        or args.delivery_budget_kib is not None and not 64 <= args.delivery_budget_kib <= 1024
        or args.upstream_read_kib is not None and (
            args.upstream_read_kib != 0 and not 8 <= args.upstream_read_kib <= 1024
        )
    ):
        parser.error("positive duration/repeats/concurrency and 1..64 object MiB required")
    if args.downstream_timing and (not args.profile or args.consumer != "async"):
        parser.error("downstream timing requires profiled original async consumption")
    executable = binary_path()
    if not executable.is_file():
        parser.error("build rust/media-relay with cargo build --release --locked first")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    xml = args.output.with_suffix(".contracts.xml")
    checked = subprocess.run([
        sys.executable, "-X", "utf8", "-m", "pytest", "tests/rust/test_media_relay.py",
        "-q", "--tb=short", f"--junitxml={xml}",
    ], cwd=ROOT, check=False)
    if checked.returncode:
        print("Compatibility gate failed. Performance benchmark was not started.", file=sys.stderr)
        return 1
    size = args.object_mib * MIB
    expected = expected_hash(size)
    rows = []
    layout = None
    if args.prebind_affinity:
        if sys.platform != "linux":
            parser.error("prelaunch thread budgets require Linux")
        cpus = sorted(os.sched_getaffinity(0))
        if len(cpus) < 4:
            parser.error("prelaunch thread budgets require at least four CPUs")
        layout = {"origin": [cpus[0]], "python": [cpus[-1]], "rust": [cpus[-1]],
                  "consumer": cpus[1:-1]}
    with experiment(trace=True, profile=args.profile, fair_source=args.fair_source,
                    legacy_source=args.legacy_source,
                    yield_after_first_body=args.yield_after_first_body,
                    delivery_budget_kib=args.delivery_budget_kib,
                    upstream_read_kib=args.upstream_read_kib,
                    startup_fairness=args.startup_fairness,
                    relay_args=["--async-log"] if args.async_log else [],
                    cpu_layout=layout) as instances:
        affinity = {}
        if layout is not None:
            from scripts.diagnose_rust_affinity import bind_threads
            from scripts.diagnose_rust_calibrated import verify_budget
            bind_threads(os.getpid(), layout["consumer"])
            verify_budget(instances, layout)
            affinity = layout
        elif hasattr(psutil.Process(), "cpu_affinity"):
            allowed = psutil.Process().cpu_affinity()
            # Give both candidates the same one-core budget; keep the source separate.
            relay_cpu = allowed[-1]
            origin_cpu = allowed[0]
            for mode in ("python", "rust", "origin"):
                process = psutil.Process(instances[mode].process.pid)
                process.cpu_affinity([origin_cpu if mode == "origin" else relay_cpu])
                affinity[mode] = process.cpu_affinity()
            if len(allowed) > 2:
                psutil.Process().cpu_affinity(allowed[1:-1])
                affinity["consumer"] = psutil.Process().cpu_affinity()
        for mode in ("python", "rust"):
            asyncio.run(transfer_load(
                instances[mode].url + "/t4/media/file", concurrency=8,
                duration=.1, size=size, expected=expected,
                trace_prefix=f"warmup-{mode}-{time.time_ns()}",
            ))
        for concurrency in args.concurrency:
            for repeat in range(args.repeats):
                order = (
                    ("origin", "python", "rust")
                    if repeat % 2 == 0
                    else ("origin", "rust", "python")
                )
                for mode in order:
                    load = measured_load
                    if args.downstream_timing and concurrency == 16 and mode != "origin":
                        from scripts.diagnose_rust_convoy import measured_downstream_load
                        load = measured_downstream_load
                    row = load(
                        instances, mode, concurrency=concurrency,
                        duration=args.duration, size=size, expected=expected,
                        consumer_mode=args.consumer,
                        profile=args.profile,
                        min_requests=args.min_requests,
                    )
                    row["repeat"] = repeat
                    rows.append(row)
                    if layout is not None:
                        verify_budget(instances, layout)
                    print(json.dumps({key: row[key] for key in (
                        "mode", "concurrency", "repeat", "mib_per_second",
                        "cpu_seconds_per_gib", "rss_peak", "ttfb_p95_ms",
                    )}), flush=True)
    report = {
        "scope": "synthetic-loopback-raw-byte-passthrough",
        "platform": platform.platform(), "python": platform.python_version(),
        "consumer_mode": args.consumer,
        "profile_enabled": args.profile,
        "downstream_timing": args.downstream_timing,
        "fair_source": args.fair_source,
        "object_mib": args.object_mib,
        "minimum_requests": args.min_requests,
        "source_cancellation_fixed": not args.legacy_source,
        "yield_after_first_body": args.yield_after_first_body,
        "delivery_budget_kib": args.delivery_budget_kib,
        "upstream_read_kib": args.upstream_read_kib,
        "startup_fairness": args.startup_fairness,
        "async_log": args.async_log,
        "cpu_budget_mode": "all-threads-prelaunch" if layout else "historical-main-thread-only",
        "cpu_affinity": affinity,
        "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                ROOT / "rust/media-relay/src/main.rs", ROOT / "rust/media-relay/Cargo.lock",
                ROOT / "rust/media-relay/src/upstream.rs",
                ROOT / "rust/media-relay/src/startup.rs",
                ROOT / "scripts/benchmark_rust_media.py", ROOT / "tests/rust/relay_support.py",
            ]
        },
        "compatibility_xml": str(xml.relative_to(ROOT)) if xml.is_relative_to(ROOT) else xml.name,
        "compatibility_passed": True,
        "local_gate": gate(rows, contracts_passed=True, repeats=args.repeats),
        "production_ready": False,
        "remaining_gates": [
            "Linux-NAS-same-hardware-and-cgroup-measurement",
            "HTTPS-real-upstream-and-AT-proxy-parity",
            "HLS-rewrite-and-special-segment-paths-remain-Python",
            "long-play-seek-pause-next-episode-real-clients",
            "production-integration-and-whole-container-net-benefit",
        ],
        "rows": rows,
    }
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"local_gate": report["local_gate"], "production_ready": False}), flush=True)
    return 0 if report["local_gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
