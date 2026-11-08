"""Acquire once per selected plugin; compare fixed bytes, not plugin availability counts."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack, nullcontext
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import statistics
import sys
import time
import traceback
import weakref
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import httpcore
import psutil

from scripts.benchmark_rust_media import cpu_seconds, percentile
from scripts.benchmark_rust_media import memory as relay_memory
from scripts.benchmark_rust_real_media import classify, relays
from scripts.check_subscription_samples import SampleRunner, prefix_read, run
from scripts.diagnose_rust_calibrated import kernel_snapshot, runtime_identity
from scripts.diagnose_rust_affinity import thread_masks, verify_masks
from tests.rust.relay_support import MIB, binary_path


def safe_failure(exc, stage):
    """Keep ownership evidence without exception messages, source text or locals."""
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        chain.append({"type": type(current).__name__, "frames": [
            {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
            for frame in traceback.extract_tb(current.__traceback__)[-12:]
        ]})
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return {"error": type(exc).__name__, "failure_stage": stage, "error_chain": chain}


def tree_snapshot():
    parent = psutil.Process()
    result = {"cpu_seconds": 0, "pss": 0, "rss_sum": 0, "processes": 0}
    for proc in (parent, *parent.children(recursive=True)):
        try:
            result["cpu_seconds"] += cpu_seconds(proc)
            full = proc.memory_full_info()
            result["rss_sum"] += full.rss
            result["pss"] += getattr(full, "pss", 0)
            result["processes"] += 1
        except psutil.NoSuchProcess:
            continue
    return result


def comparison(rows, repeats, *, modes=("python", "rust")):
    result = []
    for concurrency in (1, 4, 8, 16):
        selected = [r for r in rows if r["concurrency"] == concurrency]
        cells = {(r["repeat"], r["mode"]): r for r in selected}
        expected = {(repeat, mode) for repeat in range(repeats) for mode in modes}
        valid_metrics = all(
            isinstance(r.get(field), (int, float)) and math.isfinite(r[field]) and r[field] > 0
            for r in selected for field in ("ttfb_p95_ms", "mib_per_second")
        )
        if (repeats < 3 or len(selected) != repeats * 2 or set(cells) != expected
                or not all(r["passed"] for r in selected) or not valid_metrics):
            result.append({"concurrency": concurrency, "complete": False,
                           "paired_performance_passed": False})
            continue
        deltas, ratios, pairs = [], [], []
        for repeat in range(repeats):
            py, rust = cells[repeat, modes[0]], cells[repeat, modes[1]]
            deltas.append(rust["ttfb_p95_ms"] - py["ttfb_p95_ms"])
            ratios.append(rust["mib_per_second"] / py["mib_per_second"])
            # Preserve the historical tolerances, but apply them within each paired round.
            limit = max(2, py["ttfb_p95_ms"] * .1)
            pairs.append({"repeat": repeat, "ttfb_change_ms": deltas[-1],
                          "ttfb_limit_ms": limit, "throughput_ratio": ratios[-1],
                          "passed": deltas[-1] <= limit and ratios[-1] >= .95})
        result.append({"concurrency": concurrency, "complete": True,
                       "paired_ttfb_changes_ms": deltas, "median_change_ms": statistics.median(deltas),
                       "paired_throughput_ratios": ratios, "median_throughput_ratio": statistics.median(ratios),
                       "pairs": pairs, "failed_repeats": [p["repeat"] for p in pairs if not p["passed"]],
                       "paired_performance_passed": all(p["passed"] for p in pairs)})
    return result


def pool_effect_comparison(control, candidate, repeats):
    return [{"relay_mode": mode, "control_policy": "original", "candidate_policy": "reserved_http1",
             "comparisons": comparison([
                 {**row, "mode": policy} for policy, rows in (
                     ("original", control), ("reserved_http1", candidate))
                 for row in rows if row["mode"] == mode
             ], repeats, modes=("original", "reserved_http1"))}
            for mode in ("python", "rust")]


def batch_modes(repeat, *, aa=False, probe=False):
    if aa and not probe:
        # Each three-round block balances positions; six also balance predecessors.
        orders = (("python", "rust", "python_aa"), ("rust", "python_aa", "python"),
                  ("python_aa", "python", "rust"), ("python_aa", "rust", "python"),
                  ("python", "python_aa", "rust"), ("rust", "python", "python_aa"))
        return orders[repeat % len(orders)]
    return ("python", "rust") if repeat % 2 == 0 else ("rust", "python")


BATCH_ORDER_DESIGN = "three-position-latin-six-predecessor-balanced-v1"


def byte_contract_matches(samples, reference):
    return bool(samples) and all(
        s["passed"] and all(s.get(key) == reference.get(key)
                            for key in ("sha256", "status", "range_metadata", "mime", "bytes"))
        for s in samples
    )


def idle_pool_state(pools):
    return [{"connections": len(pool.connections), "idle": sum(c.is_idle() for c in pool.connections),
             "queued_or_active": len(pool._requests)} for pool in pools]


def consumer_pool_snapshot(client, url):
    if isinstance(client, list):
        rows = [consumer_pool_snapshot(c, url) for c in client]
        return {key: sum(row[key] for row in rows) for key in rows[0]}
    pool = client._transport_for_url(url)._pool
    origin = httpcore.Origin(url.raw_scheme, url.raw_host, url.port or (443 if url.scheme == "https" else 80))
    matching = [c for c in pool.connections if c.can_handle_request(origin)]
    return {"connections": len(pool.connections), "target_connections": len(matching),
            "target_idle": sum(c.is_idle() for c in matching),
            "queued_or_active": len(pool._requests)}


class ConsumerConnections:
    """Diagnostic identities are scoped to live streams, never file descriptors alone."""

    def __init__(self):
        self.streams = weakref.WeakKeyDictionary()
        self.sequence = 0

    def begin(self):
        self.before = {entry[0] for entry in self.streams.values()}
        self.responses = []
        self.connects = 0
        self.missing = 0

    async def request(self, request):
        async def trace(name, info):
            if name == "connection.connect_tcp.complete":
                self.connects += 1
        request.extensions["trace"] = trace

    async def response(self, response):
        stream = response.extensions.get("network_stream")
        sock = stream.get_extra_info("socket") if stream is not None else None
        if sock is None:
            self.missing += 1
            return
        previous = self.streams.get(stream)
        if previous is None or previous[1] is not sock:
            self.sequence += 1
            previous = (self.sequence, sock)
            self.streams[stream] = previous
        self.responses.append(previous[0])

    def snapshot(self):
        return {"response_connection_ids": list(self.responses),
                "unique_connections": len(set(self.responses)),
                "reused_responses": sum(key in self.before for key in self.responses),
                "new_tcp_connections": self.connects, "unobserved_responses": self.missing}


async def consumer_worker(pool_layout="shared"):
    """Persistent, bounded byte reader; stdout carries metrics only, never targets."""
    from scripts.diagnose_rust_gateway import BatchRuntimeTiming, GatewayCPUProfile
    logging.disable(logging.CRITICAL)
    identities = ConsumerConnections()
    count, capacity = (16, 1) if pool_layout == "per_stream" else (1, 16)
    async with AsyncExitStack() as stack:
        clients = [await stack.enter_async_context(httpx.AsyncClient(
            trust_env=False, follow_redirects=True, timeout=25,
            limits=httpx.Limits(max_connections=capacity, max_keepalive_connections=capacity),
            event_hooks={"request": [identities.request], "response": [identities.response]})) for _ in range(count)]
        scan_identity = None
        if pool_layout == "elide_scan":
            from scripts.diagnose_rust_idle_scan import ElideIdleScan
            scan = stack.enter_context(ElideIdleScan([client._transport._pool for client in clients]))
            scan_identity = scan.identity
        print(json.dumps({"ready": True, "pid": os.getpid(),
                          "cpus": psutil.Process().cpu_affinity(),
                          "event_loop": type(asyncio.get_running_loop()).__module__ + "."
                          + type(asyncio.get_running_loop()).__qualname__, "pool_layout": pool_layout,
                          "idle_scan_experiment": scan_identity}), flush=True)
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                request = json.loads(line)
                concurrency, limit = request["concurrency"], request["limit"]
                if not 1 <= concurrency <= 16 or not 1 <= limit <= 2 * MIB:
                    raise ValueError("bounded workload required")
                target_request = clients[0].build_request("GET", request["url"])
                pool_before = consumer_pool_snapshot(clients, target_request.url)
                identities.begin()
                profiler = None
                runtime_timing = None
                if request.get("runtime_timing", False):
                    if request.get("profile_cpu", False):
                        raise ValueError("one observer per batch")
                    runtime_timing = BatchRuntimeTiming([
                        client._transport_for_url(target_request.url)._pool for client in clients])
                if request.get("profile_cpu", False):
                    profiler = GatewayCPUProfile()
                with (profiler if profiler is not None else nullcontext(),
                      runtime_timing if runtime_timing is not None else nullcontext()):
                    before_cpu = cpu_seconds(psutil.Process())
                    window_started_ns = time.perf_counter_ns()
                    main_cpu_started_ns = time.thread_time_ns()
                    process_cpu_started_ns = time.process_time_ns()
                    started = time.perf_counter()
                    async with asyncio.timeout(60):
                        samples = await asyncio.gather(*(
                            prefix_read(clients[index] if count > 1 else clients[0], request["url"], {}, limit,
                                        opaque_verified=request.get("opaque_verified", False),
                                        complete_range=request.get("complete_range", False),
                                        trace=f"{request['trace_prefix']}-{index}"
                                        if request.get("trace_prefix") else None)
                            for index in range(concurrency)
                        ))
                    elapsed = time.perf_counter() - started
                    process_cpu_ns = time.process_time_ns() - process_cpu_started_ns
                    main_cpu_ns = time.thread_time_ns() - main_cpu_started_ns
                    window_finished_ns = time.perf_counter_ns()
                    used_cpu = cpu_seconds(psutil.Process()) - before_cpu
                pool_after = consumer_pool_snapshot(clients, target_request.url)
                result = {"samples": samples, "seconds": elapsed,
                          "consumer_connections": identities.snapshot(),
                          "consumer_pool_before": pool_before, "consumer_pool_after": pool_after,
                          "consumer_cpu_seconds": used_cpu,
                          "consumer_clock_window": {
                              "schema": "nested-batch-cpu-clocks-v1",
                              "started_ns": window_started_ns, "finished_ns": window_finished_ns,
                              "wall_ns": window_finished_ns - window_started_ns,
                              "main_cpu_ns": main_cpu_ns, "process_cpu_ns": process_cpu_ns,
                              "main_clock_resolution_ns": time.get_clock_info("thread_time").resolution * 1e9,
                              "process_clock_resolution_ns": time.get_clock_info("process_time").resolution * 1e9,
                              "valid": 0 <= main_cpu_ns <= window_finished_ns - window_started_ns,
                          }}
                if profiler is not None:
                    result["consumer_cpu_profile"] = profiler.result
                if runtime_timing is not None:
                    result["consumer_runtime_timing"] = runtime_timing.result
            except Exception as exc:
                result = {"error": type(exc).__name__}
            print(json.dumps(result), flush=True)


class ProcessConsumer:
    def __init__(self, *, loop="asyncio", pool_layout="shared"):
        if loop not in {"asyncio", "uvicorn"} or pool_layout not in {"shared", "per_stream", "elide_scan"}:
            raise ValueError("unsupported consumer configuration")
        self.loop, self.pool_layout = loop, pool_layout

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--consumer-worker",
            "--loop", self.loop, "--pool-layout", self.pool_layout,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024,
        )
        self.identity = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 20))
        assert self.identity["ready"] and self.identity["pid"] != os.getpid()
        assert set(self.identity["cpus"]) == set(psutil.Process().cpu_affinity())

    async def batch(self, url, concurrency, limit, opaque_verified=False, *, trace_prefix=None,
                    complete_range=False, profile_cpu=False, runtime_timing=False):
        message = {"url": url, "concurrency": concurrency, "limit": limit,
                   "opaque_verified": opaque_verified, "trace_prefix": trace_prefix,
                   "complete_range": complete_range, "profile_cpu": profile_cpu,
                   "runtime_timing": runtime_timing}
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()
        result = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 70))
        if "error" in result:
            raise RuntimeError("consumer failed: " + result["error"])
        return result

    async def close(self):
        if process := getattr(self, "process", None):
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()


class PerformanceRunner(SampleRunner):
    def __init__(self, *args, performance, records, **kwargs):
        super().__init__(*args, **kwargs)
        self.performance, self.records = performance, records
        self.gateway_probe = getattr(performance, "gateway_probe", None)

    async def _check(self, source, plugin, row, options, base):
        self.selected_plugin = plugin.plugin_id
        return await super()._check(source, plugin, row, options, base)

    async def _record_batch(self, result, row, child):
        bucket = ("pool_" if row["pool_policy"] == "reserved_http1" else "")
        bucket += "aa_rows" if row["mode"] == "python_aa" else "profile_rows" if row["probe"] else "rows"
        result[bucket].append(row)
        if row["probe"]:
            try:
                row["timeline"] = await self.gateway_probe.collect(
                    row["samples"], child, row["mode"],
                )
                row["timeline_complete"] = len(row["timeline"]) == row["concurrency"] and all(
                    v["complete"] for v in row["timeline"]
                )
                if timing := row.get("gateway_body_timing"):
                    owners = timing["owners"]
                    row["body_timing_complete"] = (
                        timing.get("receive_installed_before_connect", False)
                        and owners["anyio_data_received"]["calls"] > 0
                        and owners["asgi_body_send"]["bytes"] == row["bytes"]
                        and owners["transport_body_write"]["bytes"] == row["bytes"]
                    )
                    if not row["body_timing_complete"]:
                        row["diagnostic_failure"] = {"error": "incomplete_gateway_body_timing",
                                                     "failure_stage": "body_timing_coverage"}
            except Exception as exc:
                row["timeline_complete"] = False
                row["diagnostic_failure"] = safe_failure(exc, "gateway_timeline_collect")
        with self.performance.output.with_suffix(".rows.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(row) + "\n")
        print(json.dumps({key: row[key] for key in (
            "mode", "repeat", "concurrency", "probe", "pool_policy", "passed", "mib_per_second", "ttfb_p95_ms",
        )}), flush=True)

    async def compare(self, asset, *, opaque_verified=False):
        result = {"status": "fail", "scope": "fixed-delivered-media-byte-performance",
                  "plugin_id": self.selected_plugin,
                  "route": classify(asset["url"]), "rows": [], "profile_rows": [],
                  "pool_rows": [], "pool_profile_rows": [],
                  "aa_rows": [], "pool_aa_rows": [], "warmups": [],
                  "real_device_verified": False}
        self.records.append(result)
        limit = self.performance.window_mib * MIB
        context = relays(asset, profile=self.gateway_probe is not None)
        instances = None
        consumer = ProcessConsumer() if self.performance.consumer_process else None
        stage = "relay_start"
        try:
            instances = await asyncio.to_thread(context.__enter__)
            if consumer is not None:
                stage = "consumer_start"
                await consumer.start()
                result["consumer"] = consumer.identity
            pids = [os.getpid(), *(child.process.pid for child in instances.values())]
            if consumer is not None:
                pids.append(consumer.process.pid)
            budgets = {pid: psutil.Process(pid).cpu_affinity() for pid in pids}
            result["cpu_budgets"] = budgets
            def verify_threads():
                for pid, cpus in budgets.items():
                    verify_masks(thread_masks(pid), pid, cpus, [], "all-threads")
            verify_threads()
            async with httpx.AsyncClient(trust_env=False, follow_redirects=True, timeout=25,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=16)) as client:
                stage = "reference_media_read"
                reference = await prefix_read(client, asset["url"], asset["headers"], limit,
                                              opaque_verified=opaque_verified)
                result["reference"] = reference
                if reference.get("status") == 429:
                    self.stop_throttle()
                if not reference["passed"]:
                    return result
                deadline = time.monotonic() + 600
                if getattr(self.performance, "connection_warmup", False):
                    warmup_bytes = min(64 * 1024, limit)
                    stage = "warmup_reference"
                    warm_reference = await prefix_read(client, asset["url"], asset["headers"], warmup_bytes,
                                                       opaque_verified=opaque_verified, complete_range=True)
                    result["warmup_reference"] = warm_reference
                    if warm_reference.get("status") == 429:
                        self.stop_throttle()
                    if not warm_reference["passed"]:
                        return result
                for concurrency in (1, 4, 8, 16):
                    if getattr(self.performance, "only_concurrency", None) not in (None, concurrency):
                        continue
                    conditions = [
                        (repeat, probe, reserved) for repeat in range(self.performance.repeats)
                        for reserved in (((False, True) if repeat % 2 == 0 else (True, False))
                                         if getattr(self.performance, "gateway_pool_reservation", False) else (False,))
                        for probe in (((False, True) if repeat % 2 == 0 else (True, False))
                                      if self.gateway_probe is not None else (False,))
                    ]
                    for repeat, probe, reserved in conditions:
                        modes = batch_modes(repeat, probe=probe,
                                            aa=getattr(self.performance, "same_implementation_aa", False))
                        for mode in modes:
                            stage = "batch_prepare"
                            assert time.monotonic() < deadline, "asset time budget"
                            verify_threads()
                            await asyncio.sleep(1)
                            if getattr(self.performance, "gateway_pool_reservation", False):
                                from scripts.diagnose_rust_pool import set_reservation
                                set_reservation(self.performance.gateway_pools, reserved)
                            actual_mode = "python" if mode == "python_aa" else mode
                            target = instances[actual_mode].url + "/t4/media/real"
                            warm_index = None
                            if getattr(self.performance, "connection_warmup", False):
                                stage = "connection_warmup"
                                pools = getattr(self.performance, "gateway_pools", [])
                                warm_before = idle_pool_state(pools)
                                batch = await consumer.batch(target, concurrency, warmup_bytes, opaque_verified,
                                                             complete_range=True)
                                warm_index = len(result["warmups"])
                                consumer_pool_after = batch.get("consumer_pool_after", {})
                                valid = (byte_contract_matches(batch["samples"], warm_reference)
                                         and consumer_pool_after.get("target_idle", 0) >= concurrency
                                         and consumer_pool_after.get("queued_or_active") == 0)
                                result["warmups"].append({**batch, "mode": mode, "repeat": repeat,
                                    "probe": probe, "pool_policy": "reserved_http1" if reserved else "original",
                                    "concurrency": concurrency, "passed": valid,
                                    "bytes": sum(s["bytes"] for s in batch["samples"]),
                                    "consumer_pool_before": batch.get("consumer_pool_before"),
                                    "consumer_pool_after": consumer_pool_after,
                                    "pool_before": warm_before, "pool_after": idle_pool_state(pools)})
                                if any(s.get("status") == 429 for s in batch["samples"]):
                                    self.stop_throttle()
                                if not valid:
                                    return result
                                verify_threads()
                            before = tree_snapshot()
                            kernel_before = kernel_snapshot(os.sched_getaffinity(0))
                            proc = psutil.Process(instances[actual_mode].process.pid)
                            before_cpu = cpu_seconds(proc)
                            memory_before = relay_memory(proc)
                            consumer_cpu = None
                            stage = "media_batch_read"
                            cpu_profile = None
                            stack_sampler = None
                            sync_timing = None
                            cpu_sampler = None
                            body_timing = None
                            if probe and concurrency == 16 and getattr(self.performance, "gateway_cpu_profile", False):
                                from scripts.diagnose_rust_gateway import GatewayCPUProfile
                                cpu_profile = GatewayCPUProfile()
                            if probe and concurrency == 16 and getattr(self.performance, "gateway_stack_sample", False):
                                from scripts.diagnose_rust_gateway import GatewayStackSampler
                                stack_sampler = GatewayStackSampler()
                            if probe and concurrency == 16 and getattr(self.performance, "gateway_sync_timing", False):
                                from scripts.diagnose_rust_gateway import GatewaySyncTiming
                                sync_timing = GatewaySyncTiming()
                            if probe and concurrency == 16 and getattr(self.performance, "gateway_cpu_sample", False):
                                from scripts.diagnose_rust_gateway import GatewayCPUSampler
                                cpu_sampler = GatewayCPUSampler()
                            if probe and concurrency == 16 and getattr(self.performance, "gateway_body_timing", False):
                                from scripts.diagnose_rust_gateway import GatewayBodyTiming
                                body_timing = GatewayBodyTiming(receive_tap=self.performance.gateway_receive_tap)
                            with (cpu_profile if cpu_profile is not None else nullcontext(),
                                  stack_sampler if stack_sampler is not None else nullcontext(),
                                  sync_timing if sync_timing is not None else nullcontext(),
                                  cpu_sampler if cpu_sampler is not None else nullcontext(),
                                  body_timing if body_timing is not None else nullcontext()):
                                if consumer is not None:
                                    batch = await consumer.batch(target, concurrency, limit, opaque_verified,
                                        complete_range=getattr(self.performance, "complete_range", False),
                                        trace_prefix=f"gw-{concurrency}-{repeat}-{mode}-{int(reserved)}" if probe else None)
                                    samples, elapsed = batch["samples"], batch["seconds"]
                                    consumer_cpu = batch["consumer_cpu_seconds"]
                                else:
                                    started = time.perf_counter()
                                    samples = await asyncio.gather(*(
                                        prefix_read(client, target, {}, limit, opaque_verified=opaque_verified,
                                                    complete_range=getattr(self.performance, "complete_range", False))
                                        for _ in range(concurrency)
                                    ))
                                    elapsed = time.perf_counter() - started
                            verify_threads()
                            after = tree_snapshot()
                            total = sum(s["bytes"] for s in samples)
                            passed = byte_contract_matches(samples, reference)
                            latencies = [s["ttfb_ms"] for s in samples if "ttfb_ms" in s]
                            row = {"plugin_id": self.selected_plugin,
                                   "mode": mode, "repeat": repeat, "concurrency": concurrency, "probe": probe,
                                   "pool_policy": "reserved_http1" if reserved else "original",
                                   "passed": passed, "bytes": total, "seconds": elapsed,
                                   "mib_per_second": total / MIB / elapsed,
                                   "ttfb_p95_ms": percentile(latencies, .95) if latencies else None,
                                   "relay_cpu_seconds": cpu_seconds(proc) - before_cpu,
                                   "consumer_cpu_seconds": consumer_cpu,
                                   "thread_budgets_verified": True,
                                   "relay_memory_before": memory_before, "relay_memory_after": relay_memory(proc),
                                   "candidate_tree_before": before, "candidate_tree_after": after,
                                   "kernel_before": kernel_before,
                                   "kernel_after": kernel_snapshot(os.sched_getaffinity(0)),
                                   "consumer_pool_before": batch.get("consumer_pool_before") if consumer is not None else None,
                                   "consumer_pool_after": batch.get("consumer_pool_after") if consumer is not None else None,
                                   "consumer_connections": batch.get("consumer_connections") if consumer is not None else None,
                                   "complete_range": getattr(self.performance, "complete_range", False),
                                   "samples": samples, "warmup_index": warm_index}
                            if cpu_profile is not None:
                                row["gateway_cpu_profile"] = cpu_profile.result
                            if stack_sampler is not None:
                                row["gateway_stack_sample"] = stack_sampler.result
                            if sync_timing is not None:
                                row["gateway_sync_timing"] = sync_timing.result
                            if cpu_sampler is not None:
                                row["gateway_cpu_sample"] = cpu_sampler.result
                            if body_timing is not None:
                                row["gateway_body_timing"] = body_timing.result
                            stage = "record_batch"
                            await self._record_batch(result, row, instances[actual_mode])
                            if any(s.get("status") == 429 for s in samples):
                                self.stop_throttle()
                            if not passed or self.throttle_seen:
                                result["upstream_recheck"] = await prefix_read(
                                    client, asset["url"], asset["headers"], limit,
                                    opaque_verified=opaque_verified,
                                ) if not self.throttle_seen else {"skipped": "429"}
                                return result
                            if probe and (not row["timeline_complete"] or not row.get("body_timing_complete", True)):
                                result.update(row.get("diagnostic_failure", {
                                    "error": "incomplete_gateway_timeline",
                                    "failure_stage": "gateway_timeline_collect",
                                }))
                                return result
                result["status"] = "pass"
        except Exception as exc:
            result.update(safe_failure(exc, stage))
        finally:
            result["comparisons"] = comparison(result["rows"], self.performance.repeats)
            if getattr(self.performance, "same_implementation_aa", False):
                result["same_implementation_aa"] = []
                for policy, rows, aa_rows in (("original", result["rows"], result["aa_rows"]),
                                             ("reserved_http1", result["pool_rows"], result["pool_aa_rows"])):
                    aa = [row for row in rows if row["mode"] == "python"] + aa_rows
                    result["same_implementation_aa"].append({
                        "pool_policy": policy,
                        "comparisons": comparison(aa, self.performance.repeats,
                                                  modes=("python", "python_aa")),
                    })
            if getattr(self.performance, "gateway_pool_reservation", False):
                result["pool_comparisons"] = comparison(result["pool_rows"], self.performance.repeats)
                result["pool_effect"] = pool_effect_comparison(
                    result["rows"], result["pool_rows"], self.performance.repeats,
                )
            if consumer is not None:
                await consumer.close()
            if instances is not None:
                await asyncio.to_thread(context.__exit__, None, None, None)
        return result


async def audit(args):
    records = []
    if getattr(args, "gateway_profile", False):
        from scripts.diagnose_rust_gateway import GatewayTimeline
        args.gateway_probe = GatewayTimeline()
    report = {"scope": "fixed-media-paired-performance", "production_ready": False,
              "batch_order_design": BATCH_ORDER_DESIGN,
              "only_concurrency": getattr(args, "only_concurrency", None),
              "connection_warmup": getattr(args, "connection_warmup", False),
              "same_implementation_aa": getattr(args, "same_implementation_aa", False),
              "consumer_completion": "exact-range-eof" if getattr(args, "complete_range", False)
              else "historical-prefix-break",
              "consumer_topology": "separate-process-shared-cpu-budget" if args.consumer_process
              else "shared-gateway-process-and-event-loop",
              "runtime": {**runtime_identity(), "gateway_event_loop":
                          type(asyncio.get_running_loop()).__module__ + "." + type(asyncio.get_running_loop()).__qualname__},
              "gateway_loop_requested": getattr(args, "gateway_loop", "asyncio"), "assets": records,
              "gateway_profile": getattr(args, "gateway_profile", False),
              "gateway_pool_reservation": getattr(args, "gateway_pool_reservation", False),
              "gateway_cpu_profile": getattr(args, "gateway_cpu_profile", False),
              "gateway_stack_sample": getattr(args, "gateway_stack_sample", False),
              "gateway_sync_timing": getattr(args, "gateway_sync_timing", False),
              "gateway_cpu_sample": getattr(args, "gateway_cpu_sample", False),
              "gateway_body_timing": getattr(args, "gateway_body_timing", False),
              "body_timing_off_overhead": "one-inactive-receive-dispatcher-branch"
              if getattr(args, "gateway_body_timing", False) else None,
              "sniffio_version": getattr(sys.modules.get("sniffio"), "__version__", None),
              "profile_scope": "header-controlled-sampling-with-constant-gateway-and-proxy-lag-tickers-not-production"
              if getattr(args, "gateway_profile", False) else None,
              "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
              "container_scope": "isolated-full-candidate-app-plus-test-consumer-and-both-relays-not-production-net-savings"}
    acquire = SimpleNamespace(output=args.output.with_suffix(".acquisition.json"),
                              select=args.select, rounds=1, interval=1, no_relay=False)
    def factory(*a, **kw):
        return PerformanceRunner(*a, performance=args, records=records, **kw)
    def configure(app):
        if getattr(args, "gateway_pool_reservation", False):
            from scripts.diagnose_rust_pool import install_reservation
            args.gateway_pools = install_reservation(app)
        args.gateway_probe.install(app)
    try:
        receive_context = nullcontext()
        if getattr(args, "gateway_body_timing", False):
            from scripts.diagnose_rust_gateway import GatewayReceiveTap
            receive_context = GatewayReceiveTap()
        with receive_context as tap:
            args.gateway_receive_tap = tap
            acquisition = await run(acquire, runner_factory=factory, **(
                {"configure_app": configure} if getattr(args, "gateway_probe", None) else {}
            ))
        report["acquisition"] = [{"plugin_id": p["plugin_id"], "media_passed": p.get("sampled_media_passed"),
                                  "performance_status": p.get("byte_relay", {}).get("status")}
                                 for batch in acquisition["rounds"] for p in batch["plugins"]]
        report["stopped_for_throttle"] = acquisition.get("stopped_for_throttle")
        report["complete"] = len(records) == len(args.select) and all(r["status"] == "pass" for r in records)
    except Exception as exc:
        report["error"] = type(exc).__name__
    finally:
        report["aa_verified_bytes"] = sum(
            r["bytes"] for a in records for key in ("aa_rows", "pool_aa_rows")
            for r in a.get(key, []) if r["passed"])
        report["warmup_verified_bytes"] = sum(
            r["bytes"] for a in records for r in a.get("warmups", []) if r["passed"])
        report["verified_bytes"] = sum(r["bytes"] for a in records for r in a["rows"] if r["passed"])
        report["profile_verified_bytes"] = sum(
            r["bytes"] for a in records for r in a.get("profile_rows", []) if r["passed"]
        )
        report["byte_contracts_passed"] = bool(report.get("complete"))
        report["paired_performance_passed"] = report["byte_contracts_passed"] and all(
            row["paired_performance_passed"]
            for asset in records for row in asset["comparisons"]
        )
        if getattr(args, "gateway_pool_reservation", False):
            report["pool_verified_bytes"] = sum(
                r["bytes"] for a in records for r in a["pool_rows"] if r["passed"]
            )
            report["pool_profile_verified_bytes"] = sum(
                r["bytes"] for a in records for r in a["pool_profile_rows"] if r["passed"]
            )
            report["candidate_paired_performance_passed"] = report["byte_contracts_passed"] and all(
                row["paired_performance_passed"] for asset in records for row in asset["pool_comparisons"]
            )
            report["pool_no_regression_passed"] = report["byte_contracts_passed"] and all(
                row["paired_performance_passed"] for asset in records
                for mode in asset["pool_effect"] for row in mode["comparisons"]
            )
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if getattr(args, "gateway_pool_reservation", False):
        return 0 if report["candidate_paired_performance_passed"] and report["pool_no_regression_passed"] else 2
    return 0 if report["paired_performance_passed"] else 2


def run_audit(args):
    factory = None
    if args.gateway_loop == "uvicorn":
        import uvicorn
        factory = uvicorn.Config("unused:app").get_loop_factory()
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(audit(args))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--select", action="append", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--window-mib", type=int, default=1)
    parser.add_argument("--consumer-process", action="store_true")
    parser.add_argument("--gateway-profile", action="store_true")
    parser.add_argument("--gateway-pool-reservation", action="store_true")
    parser.add_argument("--gateway-cpu-profile", action="store_true")
    parser.add_argument("--gateway-stack-sample", action="store_true")
    parser.add_argument("--gateway-sync-timing", action="store_true")
    parser.add_argument("--gateway-cpu-sample", action="store_true")
    parser.add_argument("--gateway-body-timing", action="store_true")
    parser.add_argument("--only-concurrency", type=int, choices=(1, 4, 8, 16))
    parser.add_argument("--connection-warmup", action="store_true")
    parser.add_argument("--complete-range", action="store_true")
    parser.add_argument("--same-implementation-aa", action="store_true")
    parser.add_argument("--gateway-loop", choices=("asyncio", "uvicorn"), default="asyncio")
    args = parser.parse_args()
    if not 3 <= args.repeats <= 4 or not 1 <= args.window_mib <= 2 or len(args.select) > 4:
        parser.error("bounded audit: 3-4 rounds, 1-2 MiB, at most four selected plugins")
    if args.gateway_profile and (not args.consumer_process or len(args.select) != 1
                                 or not args.select[0].startswith("ordinary:")):
        parser.error("gateway profiling requires one ordinary source and a separate consumer")
    if args.gateway_pool_reservation and not args.gateway_profile:
        parser.error("pool experiment requires same-request gateway profiling")
    if args.gateway_cpu_profile and not args.gateway_profile:
        parser.error("CPU ownership requires same-request gateway profiling")
    if args.gateway_stack_sample and (not args.gateway_profile or args.gateway_cpu_profile):
        parser.error("stack sampling requires gateway profiling without deterministic CPU profiling")
    if args.gateway_sync_timing and (not args.gateway_profile or args.gateway_cpu_profile or args.gateway_stack_sample):
        parser.error("sync timing requires gateway profiling without other CPU observers")
    if args.gateway_cpu_sample and (not args.gateway_profile or args.gateway_cpu_profile
                                    or args.gateway_stack_sample or args.gateway_sync_timing):
        parser.error("CPU sampling requires gateway profiling without other CPU observers")
    if args.gateway_body_timing and (not args.gateway_profile or args.gateway_cpu_profile
                                     or args.gateway_stack_sample or args.gateway_sync_timing
                                     or args.gateway_cpu_sample):
        parser.error("body timing requires gateway profiling without other CPU observers")
    if args.same_implementation_aa and (not args.connection_warmup or args.repeats != 3
                                       or args.only_concurrency != 16):
        parser.error("A/A control requires warmed connections and exactly three paired rounds")
    if args.connection_warmup and not args.consumer_process:
        parser.error("connection warmup requires the persistent consumer")
    if args.output.exists() or args.output.with_suffix(".rows.jsonl").exists():
        parser.error("preserve prior evidence")
    logging.disable(logging.CRITICAL)
    return run_audit(args)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--consumer-worker"]:
        worker_parser = argparse.ArgumentParser()
        worker_parser.add_argument("--consumer-worker", action="store_true")
        worker_parser.add_argument("--loop", choices=("asyncio", "uvicorn"), default="asyncio")
        worker_parser.add_argument("--pool-layout", choices=("shared", "per_stream", "elide_scan"), default="shared")
        worker_args = worker_parser.parse_args()
        if worker_args.loop == "uvicorn":
            import uvicorn
            with asyncio.Runner(loop_factory=uvicorn.Config(lambda: None, loop="auto").get_loop_factory()) as worker:
                worker.run(consumer_worker(worker_args.pool_layout))
        else:
            asyncio.run(consumer_worker(worker_args.pool_layout))
    else:
        raise SystemExit(main())
