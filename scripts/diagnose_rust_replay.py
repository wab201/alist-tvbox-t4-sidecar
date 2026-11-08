"""Single-capture, bounded local replay; not a production performance gate."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response, StreamingResponse

from scripts.benchmark_rust_media import cpu_seconds, percentile
from scripts.benchmark_rust_real_media import range_metadata
from scripts.benchmark_rust_selected_media import (
    BATCH_ORDER_DESIGN, ProcessConsumer, batch_modes, byte_contract_matches, comparison, safe_failure,
)
from scripts.check_subscription_samples import SampleRunner, prefix_read, run
from scripts.diagnose_rust_calibrated import runtime_identity, schedstat, sched_delta as process_sched_delta
from sidecar.compatibility.probes import media_kind
from tests.rust.relay_support import Child, MIB, binary_path, python_app


def replay_origin(payload, mime):
    app = FastAPI()

    @app.api_route("/bytes", methods=["GET", "HEAD"])
    async def media(request: Request):
        match = re.fullmatch(r"bytes=0-(\d+)", request.headers.get("range", ""))
        if not match or int(match[1]) >= len(payload):
            return Response(status_code=416, headers={"Content-Range": f"bytes */{len(payload)}"})
        count = int(match[1]) + 1
        headers = {"Content-Type": mime, "Content-Length": str(count),
                   "Content-Range": f"bytes 0-{count - 1}/{len(payload)}", "Accept-Ranges": "bytes"}
        if request.method == "HEAD":
            return Response(status_code=206, headers=headers)

        async def chunks():
            for offset in range(0, count, 65536):
                yield payload[offset:min(offset + 65536, count)]
        return StreamingResponse(chunks(), status_code=206, headers=headers)

    return app


def replay_gateway(manifest, block, *, socket_fastpath=False):
    counters = {"iterator_calls": 0, "yielded_chunks": 0, "yielded_bytes": 0,
                "socket_fastpath_calls": 0}
    timing_state = {"active": None, "pools": []}

    def configure(bridge):
        for asset in bridge._assets.values():
            # Captured bytes have already passed the real gateway's normalization.
            asset.kind, asset.prefix, asset.redirectable = "segment", 0, False

        async def response_hook(response):
            original = response.aiter_bytes

            async def chunks(chunk_size=None):
                selected = chunk_size
                if chunk_size == 16384:
                    counters["iterator_calls"] += 1
                    selected = {"16k": 16384, "64k": 65536, "native": None}[block]
                async for chunk in original(selected):
                    counters["yielded_chunks"] += 1
                    counters["yielded_bytes"] += len(chunk)
                    yield chunk
            response.aiter_bytes = chunks

        for client in (bridge.media_client, bridge._local_media_client):
            client.event_hooks["response"].append(response_hook)
            if (pool := getattr(client._transport, "_pool", None)) is not None:
                timing_state["pools"].append(pool)
        if socket_fastpath:
            from scripts.diagnose_rust_socket import install_socket_lookup
            timing_state["socket_lookup_identity"] = install_socket_lookup(timing_state["pools"], counters)

    app = python_app(manifest, configure_bridge=configure)
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            try:
                yield
            finally:
                if timing_state["active"] is not None:
                    timing_state["active"].__exit__(None, None, None)
                    timing_state["active"] = None
    app.router.lifespan_context = lifespan

    @app.post("/__replay_timing/start")
    async def start_timing():
        from scripts.diagnose_rust_gateway import BatchRuntimeTiming
        if timing_state["active"] is not None:
            raise HTTPException(409, "batch timing already active")
        timing = BatchRuntimeTiming(timing_state["pools"])
        timing.__enter__()
        timing_state["active"] = timing
        return {"active": True}

    @app.post("/__replay_timing/stop")
    async def stop_timing():
        timing = timing_state["active"]
        if timing is None:
            raise HTTPException(409, "batch timing not active")
        try:
            timing.__exit__(None, None, None)
        finally:
            timing_state["active"] = None
        return timing.result

    @app.get("/__replay_metrics")
    async def metrics():
        return counters

    @app.get("/__replay_socket_identity")
    async def socket_identity():
        return timing_state.get("socket_lookup_identity")

    return app


def serve(args):
    if args.serve == "origin":
        metadata = json.loads(args.snapshot.with_suffix(".json").read_text())
        payload = args.snapshot.read_bytes()
        assert 0 < len(payload) <= MIB and hashlib.sha256(payload).hexdigest() == metadata["sha256"]
        app = replay_origin(payload, metadata["mime"])
    else:
        app = replay_gateway(args.manifest, args.block, socket_fastpath=args.gateway_socket_fastpath)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        previous = app.router.lifespan_context

        @asynccontextmanager
        async def ready(application):
            async with previous(application):
                print(json.dumps({"event": "ready", "listen": f"127.0.0.1:{sock.getsockname()[1]}"}), flush=True)
                yield
        app.router.lifespan_context = ready
        uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical")).run(sockets=[sock])


async def capture(asset, snapshot):
    async with httpx.AsyncClient(trust_env=False, follow_redirects=True, timeout=25) as client:
        async with client.stream("GET", asset["url"], headers={
            **asset["headers"], "Range": f"bytes=0-{MIB - 1}", "Accept-Encoding": "identity",
        }) as response:
            info = range_metadata(response, 0, MIB)
            if response.status_code == 429:
                return {"status": "throttled"}
            if not info["range_valid"]:
                return {"status": "invalid_range", "http_status": response.status_code}
            body = bytearray()
            async for part in response.aiter_raw():
                if len(body) + len(part) > MIB:
                    return {"status": "overflow"}
                body.extend(part)
            if len(body) != MIB or not media_kind(bytes(body[:4096])):
                return {"status": "short_or_unknown_media"}
            metadata = {"status": "pass", "bytes": len(body), "mime": info["content_type"],
                        "format": media_kind(bytes(body[:4096])),
                        "sha256": hashlib.sha256(body).hexdigest(), "response_exhausted": True}
            snapshot.write_bytes(body)
            snapshot.with_suffix(".json").write_text(json.dumps(metadata))
            return metadata


def conditions():
    return [(block, eof, warm) for block in ("16k", "64k", "native")
            for eof, warm in ([(False, False), (True, False), (False, True), (True, True)]
                              if block == "16k" else [(True, True)])]


def factorial_conditions():
    return [(concurrency, kib) for concurrency in (1, 4, 8, 16)
            for kib in (64, 256, 1024)]


def factorial_order(repeat):
    cells = factorial_conditions()
    # Coprime stride spreads nearby workloads; repeat rotation moves their positions.
    return [(index, *cells[index]) for index in
            ((position * 5 + repeat * 7) % len(cells) for position in range(len(cells)))]


def factorial_complete(rows):
    expected = {(n, kib, repeat, mode) for n, kib in factorial_conditions()
                for repeat in range(3) for mode in ("python", "rust", "python_aa")}
    actual = {(r["concurrency"], r["per_stream_kib"], r["repeat"], r["mode"]) for r in rows}
    return len(rows) == len(expected) and actual == expected and all(
        row_evidence_passed(r) and not r["profile"] and not r["runtime_timing"]
        and r["complete_range"] and r["warmup"]
        and r["bytes"] == r["concurrency"] * r["per_stream_kib"] * 1024 for r in rows)


def socket_study_complete(rows, repeats=4):
    expected = {(n, kib, repeat, fast, mode) for n, kib in ((1, 64), (16, 64), (1, 1024), (16, 1024))
                for repeat in range(repeats) for fast in (False, True) for mode in ("python", "rust")}
    actual = {(r["concurrency"], r["per_stream_kib"], r["repeat"], r["gateway_socket_fastpath"], r["mode"])
              for r in rows}
    return len(rows) == len(expected) and actual == expected and all(
        row_evidence_passed(r) and not r["profile"] and not r["runtime_timing"]
        and r["complete_range"] and r["warmup"]
        and r["bytes"] == r["concurrency"] * r["per_stream_kib"] * 1024 for r in rows)


def socket_study_effect(rows, repeats=4):
    if not socket_study_complete(rows, repeats):
        raise ValueError("complete socket lookup comparison required")
    cells = {(r["mode"], r["concurrency"], r["per_stream_kib"], r["repeat"], r["gateway_socket_fastpath"]): r
             for r in rows}
    result = []
    for mode, n, kib in ((mode, n, kib) for mode in ("python", "rust")
                        for n, kib in ((1, 64), (16, 64), (1, 1024), (16, 1024))):
        pairs = []
        for repeat in range(repeats):
            baseline, fast = cells[mode, n, kib, repeat, False], cells[mode, n, kib, repeat, True]
            delta = fast["ttfb_p95_ms"] - baseline["ttfb_p95_ms"]
            ratio = fast["mib_per_second"] / baseline["mib_per_second"]
            limit = max(2, baseline["ttfb_p95_ms"] * .1)
            pairs.append({"repeat": repeat, "baseline_ttfb_ms": baseline["ttfb_p95_ms"],
                "fast_ttfb_ms": fast["ttfb_p95_ms"], "ttfb_delta_ms": delta,
                "ttfb_limit_ms": limit, "throughput_ratio": ratio,
                "baseline_lookup_calls": baseline["gateway_counts"]["socket_fastpath_calls"],
                "fast_lookup_calls": fast["gateway_counts"]["socket_fastpath_calls"],
                "baseline_position": baseline["gateway_variant_position"],
                "fast_position": fast["gateway_variant_position"],
                "passed": ratio >= .95 and delta <= limit})
        result.append({"relay_mode": mode, "concurrency": n, "per_stream_kib": kib, "pairs": pairs,
            "failed_repeats": [p["repeat"] for p in pairs if not p["passed"]],
            "all_pairs_passed": all(p["passed"] for p in pairs)})
    return result


def row_evidence_passed(row):
    passed = (row["passed"] and row["connection_evidence_passed"]
              and (not row["profile"] or row.get("consumer_cpu_profile", {}).get("valid") is True))
    if row.get("gateway_socket_fastpath"):
        passed &= row.get("gateway_counts", {}).get("socket_fastpath_calls", 0) > 0
    if row.get("runtime_timing"):
        timings = [row.get(owner + "_runtime_timing", {}) for owner in ("gateway", "consumer")]
        for timing in timings:
            passed &= (timing.get("valid") is True and timing.get("restored") is True
                       and timing.get("main_thread_sched", {}).get("available") is True
                       and timing.get("foreign_pool_calls") == 0
                       and timing.get("gc", {}).get("truncated") is False
                       and sum(p["calls"] for p in timing.get("pools", [])) >= row["concurrency"] * 2
                       and all(not p["errors"] for p in timing.get("pools", [])))
        gateway, consumer = timings
        passed &= (0 < gateway.get("started_ns", 0) <= consumer.get("started_ns", 0)
                   < consumer.get("finished_ns", 0) <= gateway.get("finished_ns", 0))
    return bool(passed)


async def replay(snapshot, directory, report, *, loop_study=False, pool_study=False,
                 runtime_study=False, scan_study=False, factorial_study=False, socket_study=False):
    children = []
    cpus = psutil.Process().cpu_affinity()
    assert sys.platform == "linux" and len(cpus) >= 4
    launch = [sys.executable, "-X", "utf8", str(Path(__file__).resolve())]
    try:
        origin = await asyncio.to_thread(Child, [*launch, "--serve", "origin", "--snapshot", str(snapshot)],
                                         directory, "origin", cpus=[cpus[0]])
        children.append(origin)
        gateways, relays = {}, {}
        blocks = (("16k", "socket-fast") if socket_study else
                  ("16k",) if loop_study or pool_study or runtime_study or scan_study or factorial_study
                  else ("16k", "64k", "native"))
        for block in blocks:
            manifest = directory / (block + "-gateway.json")
            manifest.write_text(json.dumps({"real": {"url": origin.url + "/bytes", "headers": {}}}))
            gateway = await asyncio.to_thread(Child, [*launch, "--serve", "gateway", "--manifest", str(manifest),
                                                     "--block", "16k" if block == "socket-fast" else block,
                                                     *(["--gateway-socket-fastpath"] if block == "socket-fast" else [])],
                                                     directory, block + "-gateway", cpus=[cpus[1]])
            children.append(gateway)
            gateways[block] = gateway
            manifest = directory / (block + "-relay.json")
            manifest.write_text(json.dumps({"real": {"url": gateway.url + "/t4/media/real", "headers": {}}}))
            py = await asyncio.to_thread(Child, [sys.executable, "-X", "utf8", "-m", "tests.rust.relay_support",
                                                "--serve", "python", "--assets", str(manifest)],
                                         directory, block + "-python", cpus=[cpus[-1]])
            children.append(py)
            rust = await asyncio.to_thread(Child, [str(binary_path()), "--assets", str(manifest)],
                                           directory, block + "-rust", cpus=[cpus[-1]])
            children.append(rust)
            relays[block] = {"python": py, "rust": rust}
        report["cpu_layout"] = {"origin": [cpus[0]], "gateway": [cpus[1]],
                                "relay": [cpus[-1]], "consumer": [cpus[2]]}
        from scripts.diagnose_rust_affinity import bind_threads, thread_masks, verify_masks
        bind_threads(os.getpid(), [cpus[2]])
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=25) as monitor:
                if socket_study:
                    report["socket_lookup_identity"] = (await monitor.get(
                        gateways["socket-fast"].url + "/__replay_socket_identity")).json()
                    assert report["socket_lookup_identity"]
                reference = await prefix_read(monitor, origin.url + "/bytes", {}, MIB, complete_range=True)
                warm_reference = await prefix_read(monitor, origin.url + "/bytes", {}, 65536, complete_range=True)
                assert reference["passed"] and warm_reference["passed"]
                assert reference["sha256"] == report["capture"]["sha256"]
                references = {MIB: reference, 65536: warm_reference}
                if factorial_study:
                    references[262144] = await prefix_read(monitor, origin.url + "/bytes", {}, 262144,
                                                           complete_range=True)
                    captured = snapshot.read_bytes()
                    assert hashlib.sha256(captured).hexdigest() == report["capture"]["sha256"]
                    for limit, selected in references.items():
                        assert selected["passed"] and selected["response_exhausted"]
                        assert selected["bytes"] == limit
                        assert selected["sha256"] == hashlib.sha256(captured[:limit]).hexdigest()
                    report["prefix_references"] = {str(limit): {k: selected[k] for k in
                        ("sha256", "status", "range_metadata", "mime", "bytes")}
                        for limit, selected in references.items()}
                for repeat in range(4 if socket_study else 3):
                    plan = [(block, eof, warm, "asyncio", False, "shared") for block, eof, warm in conditions()]
                    if loop_study:
                        plan = [("16k", True, True, loop, profile, "shared") for loop in ("asyncio", "uvicorn")
                                for profile in ((False, True) if repeat == 0 else (False,))]
                    if pool_study:
                        plan = [("16k", True, True, "asyncio", profile, layout) for layout in ("shared", "per_stream")
                                for profile in ((False, True) if repeat == 0 else (False,))]
                    plan = [(*cell, False) for cell in plan]
                    if runtime_study:
                        plan = [("16k", True, True, "asyncio", False, "shared", timing) for timing in (False, True)]
                    if scan_study:
                        plan = [("16k", True, True, "asyncio", False, layout, timing)
                                for layout in ("shared", "elide_scan")
                                for timing in ((False, True) if repeat == 0 else (False,))]
                    if factorial_study:
                        plan = [("16k", True, True, "asyncio", False, "shared", False, index, n, kib)
                                for index, n, kib in factorial_order(repeat)]
                    if socket_study:
                        plan = []
                        for cell_index, (n, kib) in enumerate(((1, 64), (16, 64), (1, 1024), (16, 1024))):
                            variants = (False, True) if (repeat + cell_index) % 2 == 0 else (True, False)
                            for fast in variants:
                                plan.append(("socket-fast" if fast else "16k", True, True, "asyncio",
                                             False, "shared", False, cell_index, n, kib))
                    ordered = plan if factorial_study or socket_study or repeat % 2 == 0 else list(reversed(plan))
                    for item in ordered:
                        block, eof, warm, loop, profile, layout, runtime_timing = item[:7]
                        cell_index, concurrency, kib = item[7:] if factorial_study or socket_study else (0, 16, 1024)
                        limit = kib * 1024
                        reference = references[limit]
                        # A new consumer per cell makes 'no warmup' genuinely cold.
                        mode_round = ((repeat + cell_index) % 3 + 3 * (cell_index % 2)
                                      if factorial_study else repeat)
                        modes = batch_modes(mode_round, aa=not socket_study,
                                            probe=profile or (runtime_timing and scan_study))
                        for mode_position, mode in enumerate(modes):
                            relay = relays[block]["python" if mode == "python_aa" else mode]
                            gateway = gateways[block]
                            consumer = ProcessConsumer(loop=loop, pool_layout=layout)
                            try:
                                await consumer.start()
                                thread_evidence = {}
                                for proc, expected in ((origin.process, [cpus[0]]), (gateway.process, [cpus[1]]),
                                                       (relay.process, [cpus[-1]]), (consumer.process, [cpus[2]])):
                                    masks = thread_masks(proc.pid)
                                    verify_masks(masks, proc.pid, expected, [], "all-threads")
                                    thread_evidence[str(proc.pid)] = masks
                                target = relay.url + "/t4/media/real"
                                warm_batch = None
                                if warm:
                                    warm_batch = await consumer.batch(target, concurrency, 65536, complete_range=True)
                                    assert byte_contract_matches(warm_batch["samples"], warm_reference)
                                    assert all(s["response_exhausted"] for s in warm_batch["samples"])
                                    report["warmup_verified_bytes"] += concurrency * 65536
                                metrics_before = (await monitor.get(gateway.url + "/__replay_metrics")).json()
                                tracked = {key: psutil.Process(proc.pid) for key, proc in (
                                    ("origin", origin.process), ("gateway", gateway.process), ("relay", relay.process))}
                                if runtime_timing:
                                    (await monitor.post(gateway.url + "/__replay_timing/start")).raise_for_status()
                                try:
                                    before_sched = {key: schedstat(proc.pid) for key, proc in tracked.items()}
                                    before = {key: cpu_seconds(proc) for key, proc in tracked.items()}
                                    batch = await consumer.batch(target, concurrency, limit, complete_range=eof,
                                                                 profile_cpu=profile, runtime_timing=runtime_timing)
                                    used = {key: cpu_seconds(proc) - before[key] for key, proc in tracked.items()}
                                    owner_sched = {key: process_sched_delta(before_sched[key], schedstat(proc.pid))
                                                   for key, proc in tracked.items()}
                                finally:
                                    if runtime_timing:
                                        stopped = await monitor.post(gateway.url + "/__replay_timing/stop")
                                        stopped.raise_for_status()
                                        gateway_timing = stopped.json()
                                if runtime_timing:
                                    batch["gateway_runtime_timing"] = gateway_timing
                                metrics_after = (await monitor.get(gateway.url + "/__replay_metrics")).json()
                                identities = batch["consumer_connections"]
                                byte_pass = byte_contract_matches(batch["samples"], reference)
                                connection_pass = (identities["unobserved_responses"] == 0
                                    and 1 <= identities["unique_connections"] <= concurrency
                                    and identities["new_tcp_connections"] == (0 if warm else concurrency)
                                    and identities["reused_responses"] == (concurrency if warm else 0))
                                if warm:
                                    connection_pass &= set(identities["response_connection_ids"]) <= set(
                                        warm_batch["consumer_connections"]["response_connection_ids"])
                                row = {**batch, "block": "16k" if block == "socket-fast" else block,
                                       "gateway_socket_fastpath": block == "socket-fast",
                                       "gateway_variant_position": int((block == "socket-fast") != bool((repeat + cell_index) % 2)),
                                       "complete_range": eof, "warmup": warm,
                                       "consumer_loop": loop, "consumer_identity": consumer.identity, "profile": profile,
                                       "runtime_timing": runtime_timing,
                                       "consumer_pool_layout": layout,
                                       "mode": mode, "mode_position": mode_position,
                                       "repeat": repeat, "concurrency": concurrency, "per_stream_kib": kib,
                                       "passed": byte_pass, "connection_evidence_passed": connection_pass,
                                       "bytes": sum(s["bytes"] for s in batch["samples"]), "owner_cpu_seconds": used,
                                       "owner_thread_sched": owner_sched, "thread_affinity_before": thread_evidence,
                                       "gateway_counts": {k: metrics_after[k] - metrics_before[k] for k in metrics_before},
                                       "mib_per_second": sum(s["bytes"] for s in batch["samples"]) / MIB / batch["seconds"],
                                       "ttfb_p95_ms": percentile([s["ttfb_ms"] for s in batch["samples"]], .95)}
                                report["rows"].append(row)
                                report["verified_bytes"] += row["bytes"] if byte_pass else 0
                                print(json.dumps({k: row[k] for k in ("block", "complete_range", "warmup", "mode",
                                      "repeat", "consumer_loop", "consumer_pool_layout", "profile", "runtime_timing", "passed", "connection_evidence_passed",
                                      "concurrency", "per_stream_kib", "mode_position", "gateway_socket_fastpath", "mib_per_second")}), flush=True)
                                assert row_evidence_passed(row)
                                assert all(s["response_exhausted"] == eof for s in batch["samples"])
                            finally:
                                await consumer.close()
        finally:
            bind_threads(os.getpid(), cpus)
    finally:
        for child in reversed(children):
            await asyncio.to_thread(child.close)


async def audit(args):
    factorial = getattr(args, "factorial_study", False)
    socket_study = getattr(args, "gateway_socket_study", False)
    report = {"scope": "captured-media-local-replay-attribution", "production_ready": False,
              "batch_order_design": "gateway-AB-BA-four-round-two-mode-v1" if socket_study else BATCH_ORDER_DESIGN,
              "consumer_loop_study": args.consumer_loop_study,
              "consumer_pool_study": args.consumer_pool_study,
              "runtime_study": args.runtime_study,
              "consumer_scan_study": args.consumer_scan_study,
              "factorial_study": factorial,
              "gateway_socket_study": socket_study,
              "real_device_verified": False, "runtime": runtime_identity(), "rows": [],
              "verified_bytes": 0, "warmup_verified_bytes": 0,
              "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
              "topology": "cached-bytes-origin -> isolated-real-PlaybackBridge-segment -> relay -> consumer",
              "limitations": ["loopback-not-public-network", "instrumented-iterator",
                              "one-core-per-owner-not-production-budget", "normalized-1MiB-prefix-only",
                              "consumer-cold-per-cell-upstream-pools-persist"]}
    try:
        with tempfile.TemporaryDirectory(prefix="t4-replay-") as temporary:
            directory = Path(temporary)
            snapshot = directory / "captured.bin"

            class CaptureRunner(SampleRunner):
                async def compare(self, asset, *, opaque_verified=False):
                    if "capture" not in report:
                        report["capture"] = await capture(asset, snapshot)
                    if report["capture"]["status"] == "throttled":
                        self.stop_throttle()
                    return report["capture"]

            acquisition = await run(SimpleNamespace(output=args.output.with_suffix(".acquisition.json"),
                                    select=args.select, rounds=1, interval=1, no_relay=False),
                                    runner_factory=CaptureRunner)
            report["stopped_for_throttle"] = acquisition.get("stopped_for_throttle")
            assert report.get("capture", {}).get("status") == "pass" and not report["stopped_for_throttle"]
            await replay(snapshot, directory, report, loop_study=args.consumer_loop_study,
                         pool_study=args.consumer_pool_study, runtime_study=args.runtime_study,
                         scan_study=args.consumer_scan_study, factorial_study=factorial, socket_study=socket_study)
            expected = 64 if socket_study else 18 if args.runtime_study else 22 if (args.consumer_loop_study or args.consumer_pool_study
                                                          or args.consumer_scan_study) else 54
            report["complete"] = (len(report["rows"]) == expected
                                  and all(row_evidence_passed(row) for row in report["rows"]))
            if factorial:
                report["complete"] = factorial_complete(report["rows"])
            if socket_study:
                report["complete"] = socket_study_complete(report["rows"])
    except Exception as exc:
        report.update(safe_failure(exc, "controlled_replay"))
    finally:
        report["comparisons"] = []
        cells = [(block, eof, warm, "asyncio", "shared") for block, eof, warm in conditions()]
        if args.consumer_loop_study:
            cells = [("16k", True, True, loop, "shared") for loop in ("asyncio", "uvicorn")]
        if args.consumer_pool_study:
            cells = [("16k", True, True, "asyncio", layout) for layout in ("shared", "per_stream")]
        if args.runtime_study:
            cells = [("16k", True, True, "asyncio", "shared")]
        if args.consumer_scan_study:
            cells = [("16k", True, True, "asyncio", layout) for layout in ("shared", "elide_scan")]
        cells = [] if socket_study else [(*cell, 16, 1024) for cell in cells]
        if factorial:
            cells = [("16k", True, True, "asyncio", "shared", n, kib) for n, kib in factorial_conditions()]
        for block, eof, warm, loop, layout, n, kib in cells:
            rows = [r for r in report["rows"] if not r["profile"] and not r.get("runtime_timing") and
                    r.get("gateway_socket_fastpath", False) == (block == "socket-fast") and
                    (r["block"], r["complete_range"], r["warmup"], r["consumer_loop"], r["consumer_pool_layout"],
                     r["concurrency"], r["per_stream_kib"]) == (
                         "16k" if block == "socket-fast" else block, eof, warm, loop, layout, n, kib)]
            def selected_comparison(selected, modes):
                return next(c for c in comparison(selected, 3, modes=modes) if c["concurrency"] == n)
            report["comparisons"].append({"block": "16k" if block == "socket-fast" else block,
                "gateway_socket_fastpath": block == "socket-fast", "complete_range": eof, "warmup": warm,
                "consumer_loop": loop, "consumer_pool_layout": layout, "concurrency": n, "per_stream_kib": kib,
                "rust_vs_python": selected_comparison([r for r in rows if r["mode"] != "python_aa"], ("python", "rust")),
                "aa": selected_comparison([r for r in rows if r["mode"] != "rust"], ("python", "python_aa"))})
        if args.consumer_loop_study:
            report["consumer_loop_effect"] = [{"relay_mode": mode, "comparison": comparison(
                [{**r, "mode": r["consumer_loop"]} for r in report["rows"]
                 if r["mode"] == mode and not r["profile"]], 3, modes=("asyncio", "uvicorn"))[-1]}
                for mode in ("python", "rust", "python_aa")]
        if args.consumer_pool_study:
            report["consumer_pool_effect"] = [{"relay_mode": mode, "comparison": comparison(
                [{**r, "mode": r["consumer_pool_layout"]} for r in report["rows"]
                 if r["mode"] == mode and not r["profile"]], 3, modes=("shared", "per_stream"))[-1]}
                for mode in ("python", "rust", "python_aa")]
        if args.runtime_study:
            report["probe_effect"] = [{"relay_mode": mode, "comparison": comparison(
                [{**r, "mode": "observed" if r["runtime_timing"] else "baseline"}
                 for r in report["rows"] if r["mode"] == mode], 3, modes=("baseline", "observed"))[-1]}
                for mode in ("python", "rust", "python_aa")]
        if args.consumer_scan_study:
            report["consumer_scan_effect"] = [{"relay_mode": mode, "comparison": comparison(
                [{**r, "mode": r["consumer_pool_layout"]} for r in report["rows"]
                 if r["mode"] == mode and not r["runtime_timing"]], 3, modes=("shared", "elide_scan"))[-1]}
                for mode in ("python", "rust", "python_aa")]
        if socket_study:
            report["gateway_socket_effect"] = socket_study_effect(report["rows"]) if report.get("complete") else []
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report.get("complete") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", choices=("origin", "gateway"))
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--block", choices=("16k", "64k", "native"), default="16k")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--select", action="append")
    parser.add_argument("--consumer-loop-study", action="store_true")
    parser.add_argument("--consumer-pool-study", action="store_true")
    parser.add_argument("--runtime-study", action="store_true")
    parser.add_argument("--consumer-scan-study", action="store_true")
    parser.add_argument("--factorial-study", action="store_true")
    parser.add_argument("--gateway-socket-study", action="store_true")
    parser.add_argument("--gateway-socket-fastpath", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.serve:
        serve(args)
        return 0
    if args.gateway_socket_fastpath:
        parser.error("socket fastpath is only for the isolated gateway child")
    if not args.output or args.output.exists() or not args.select or len(args.select) != 1:
        parser.error("one selected source and an unused output path required")
    if not args.select[0].startswith("ordinary:"):
        parser.error("only the authorized ordinary source; no cloud retry")
    if sum((args.consumer_loop_study, args.consumer_pool_study, args.runtime_study,
            args.consumer_scan_study, args.factorial_study, args.gateway_socket_study)) > 1:
        parser.error("one consumer variable per study")
    factory = uvicorn.Config(lambda: None, loop="auto").get_loop_factory()
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(audit(args))


if __name__ == "__main__":
    raise SystemExit(main())
