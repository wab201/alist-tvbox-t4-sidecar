"""Opt-in, in-memory gateway timeline for isolated experiments, never production."""
from __future__ import annotations

import asyncio
import cProfile
import gc
from collections import deque
from contextlib import asynccontextmanager, ExitStack
import json
import hashlib
import inspect
import importlib
import math
from pathlib import Path
import pstats
import signal
import ssl
import sys
import threading
import time
from unittest.mock import patch

import httpx

from tests.rust.relay_support import CURRENT_PROFILE, ProfileMiddleware


TRANSPORT_EVENTS = frozenset(
    f"{stage}.{state}"
    for stage in (
        "connection.connect_tcp", "connection.start_tls", "http11.send_request_headers",
        "http11.receive_response_headers", "http2.send_request_headers",
        "http2.receive_response_headers",
    ) for state in ("started", "complete", "failed")
)


def parse_schedstat(text):
    values = [int(value) for value in text.split()]
    if len(values) != 3 or any(value < 0 for value in values):
        raise ValueError("invalid schedstat counters")
    return dict(zip(("run_ns", "runqueue_ns", "timeslices"), values))


def sched_delta(before, after):
    if before is None or after is None:
        return {"available": False}
    delta = {key: after[key] - before[key] for key in before}
    if any(value < 0 for value in delta.values()):
        return {"available": False, "counter_reset": True}
    return {"available": True, **delta}


class GatewayStackSampler:
    """Bounded statistical stacks, never locals; sampling gaps are not CPU ownership.

    The observer needs the GIL too. A stalled observer is explicitly unobserved;
    schedstat only separates running/runqueue time, not network from lock waits.
    """

    def __init__(self, *, interval=.005, capacity=4096):
        if not .001 <= interval <= .1 or not 2 <= capacity <= 8192:
            raise ValueError("bounded sampling required")
        self.interval = interval
        self.samples = deque(maxlen=capacity)
        self.stop = threading.Event()
        self.total = 0
        self.errors = 0
        self.cpu_ms = 0

    def _sched(self):
        try:
            return parse_schedstat(self.sched_path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return None

    def _sample(self, due_ns):
        now = time.perf_counter_ns()
        frame = sys._current_frames().get(self.thread_id)
        stack = []
        try:
            while frame is not None and len(stack) < 12:
                stack.append({"file": Path(frame.f_code.co_filename).name,
                              "function": frame.f_code.co_name, "line": frame.f_lineno})
                frame = frame.f_back
        finally:
            del frame
        self.samples.append({"at_us": self.started_us + (now - self.started_ns) // 1000,
                             "late_us": max(0, now - due_ns) // 1000,
                             "sched": self._sched(), "stack": stack,
                             "capture_us": (time.perf_counter_ns() - now) // 1000})
        self.total += 1

    def _watch(self):
        started = time.thread_time_ns()
        due = time.perf_counter_ns()
        try:
            while not self.stop.is_set():
                try:
                    self._sample(due)
                except Exception:
                    self.errors += 1
                due = time.perf_counter_ns() + int(self.interval * 1e9)
                if self.stop.wait(self.interval):
                    break
        except Exception:
            self.errors += 1
        finally:
            self.cpu_ms = (time.thread_time_ns() - started) / 1e6

    def __enter__(self):
        self.thread_id = threading.get_ident()
        self.native_id = threading.get_native_id()
        self.sched_path = Path(f"/proc/self/task/{self.native_id}/schedstat")
        try:
            self.schedstats_enabled = Path("/proc/sys/kernel/sched_schedstats").read_text().strip() == "1"
        except OSError:
            self.schedstats_enabled = None
        self.started_ns, self.started_us = time.perf_counter_ns(), time.time_ns() // 1000
        self.before = self._sched()
        self.worker = threading.Thread(target=self._watch, name="gateway-stack-observer", daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *args):
        finished = time.perf_counter_ns()
        after = self._sched()
        self.stop.set()
        self.worker.join(timeout=2)
        if self.worker.is_alive():
            raise RuntimeError("stack observer did not stop")
        end = self.started_us + (finished - self.started_ns) // 1000
        samples = [row for row in self.samples if row["at_us"] <= end]
        points = [self.started_us, *(row["at_us"] for row in samples), end]
        self.result = {"scope": "statistical-main-thread-stacks-not-causal-duration",
                       "interval_ms": self.interval * 1000, "started_us": self.started_us,
                       "finished_us": end, "wall_ms": (finished - self.started_ns) / 1e6,
                       "schedstats_enabled": self.schedstats_enabled,
                       "main_thread_sched": sched_delta(self.before, after),
                       "observer_cpu_ms": self.cpu_ms, "errors": self.errors,
                       "truncated": self.total > self.samples.maxlen, "sample_count": len(samples),
                       "max_sample_gap_ms": max((b - a for a, b in zip(points, points[1:])), default=0) / 1000,
                       "samples": samples}


class GatewaySyncTiming:
    """Measure two observed synchronous owners without a competing sample thread."""

    @staticmethod
    def counters():
        return {"calls": 0, "wall_ns": 0, "cpu_ns": 0, "max_wall_ns": 0,
                "bytes": 0, "errors": {}}

    def _wrap(self, name, original):
        def observed(*args, **kwargs):
            if threading.get_ident() != self.thread_id:
                return original(*args, **kwargs)
            cpu, start = time.thread_time_ns(), time.perf_counter_ns()
            count, error = 0, None
            try:
                result = original(*args, **kwargs)
                if name == "ssl_read":
                    count = result if isinstance(result, int) else len(result)
                return result
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                wall, used = time.perf_counter_ns() - start, time.thread_time_ns() - cpu
                targets = [self.owners[name]]
                event = CURRENT_PROFILE.get()
                if event is not None:
                    targets.append(event.setdefault("sync_work", {}).setdefault(name, self.counters()))
                for stats in targets:
                    stats["calls"] += 1
                    stats["wall_ns"] += wall
                    stats["cpu_ns"] += used
                    stats["bytes"] += count
                    if wall > stats["max_wall_ns"]:
                        stats["max_wall_ns"] = wall
                        stats["max_started_us"] = self.started_us + (start - self.started_ns) // 1000
                    if error:
                        stats["errors"][error] = stats["errors"].get(error, 0) + 1
        return observed

    def __enter__(self):
        import httpcore._synchronization as synchronization
        self.thread_id = threading.get_ident()
        self.started_ns, self.started_us = time.perf_counter_ns(), time.time_ns() // 1000
        self.started_cpu = time.thread_time_ns()
        self.owners = {name: self.counters() for name in ("ssl_read", "async_library")}
        self.patches = ExitStack()
        try:
            for owner, attribute, name in (
                (ssl.SSLObject, "read", "ssl_read"),
                (synchronization, "current_async_library", "async_library"),
            ):
                self.patches.enter_context(patch.object(owner, attribute, self._wrap(name, getattr(owner, attribute))))
        except BaseException:
            self.patches.close()
            raise
        return self

    def __exit__(self, *args):
        self.patches.close()
        self.result = {"scope": "synchronous-owner-only-not-await-cpu",
                       "wall_ms": (time.perf_counter_ns() - self.started_ns) / 1e6,
                       "main_cpu_ms": (time.thread_time_ns() - self.started_cpu) / 1e6,
                       "owners": self.owners}


class BatchRuntimeTiming:
    """Opt-in pool CPU, GC and schedstat for one batch, without a sampler thread.

    Pool CPU and GC can overlap; neither may be added to the batch CPU. A
    schedstat delta is a window total, not attribution to a specific request.
    """

    def __init__(self, pools):
        self.pools = list(pools)
        if not self.pools or len({id(pool) for pool in self.pools}) != len(self.pools):
            raise ValueError("unique explicit pools required")
        self.active = False

    def _sched(self):
        try:
            return parse_schedstat(self.sched_path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return None

    def _wrap(self, original, stats):
        def observed(*args, **kwargs):
            if threading.get_ident() != self.thread_id:
                self.foreign_pool_calls += 1
                return original(*args, **kwargs)
            start, cpu = time.perf_counter_ns(), time.thread_time_ns()
            try:
                return original(*args, **kwargs)
            except BaseException as exc:
                name = type(exc).__name__
                stats["errors"][name] = stats["errors"].get(name, 0) + 1
                raise
            finally:
                used, end = time.thread_time_ns() - cpu, time.perf_counter_ns()
                stats["calls"] += 1
                stats["cpu_ns"] += used
                stats["wall_ns"] += end - start
                if end - start > stats["max_wall_ns"]:
                    stats["max_wall_ns"] = end - start
                    stats["max_started_ns"] = start
        return observed

    def _gc(self, phase, info):
        if threading.get_ident() != self.thread_id:
            self.foreign_gc_callbacks += 1
            return
        generation = info["generation"]
        if phase == "start":
            if generation in self.pending_gc:
                self.gc_errors += 1
            self.pending_gc[generation] = (time.perf_counter_ns(), time.thread_time_ns())
        elif phase == "stop":
            end, cpu = time.perf_counter_ns(), time.thread_time_ns()
            start = self.pending_gc.pop(generation, None)
            if start is None:
                self.gc_errors += 1
                return
            stats = self.gc_stats
            wall, used = end - start[0], cpu - start[1]
            stats["calls"] += 1
            stats["wall_ns"] += wall
            stats["cpu_ns"] += used
            stats["max_wall_ns"] = max(stats["max_wall_ns"], wall)
            stats["by_generation"][generation] += 1
            if len(stats["events"]) < 128:
                stats["events"].append({"generation": generation, "started_ns": start[0],
                                        "finished_ns": end, "cpu_ns": used})

    def __enter__(self):
        if self.active or any(getattr(p, "_t4_batch_timing_owner", None) is not None for p in self.pools):
            raise RuntimeError("pool timing already active")
        self.thread_id = threading.get_ident()
        self.sched_path = Path(f"/proc/self/task/{threading.get_native_id()}/schedstat")
        try:
            self.schedstats_enabled = Path("/proc/sys/kernel/sched_schedstats").read_text().strip() == "1"
        except OSError:
            self.schedstats_enabled = None
        self.foreign_pool_calls = self.foreign_gc_callbacks = self.gc_errors = 0
        self.pending_gc = {}
        self.gc_stats = {**GatewaySyncTiming.counters(), "by_generation": [0, 0, 0], "events": []}
        self.pool_stats, self.originals = [], []
        self.patches = ExitStack()
        try:
            for pool in self.pools:
                original = pool._assign_requests_to_connections
                if inspect.iscoroutinefunction(original):
                    raise TypeError("synchronous pool method required")
                source = getattr(original, "_t4_timing_source", None)
                if source is None:
                    source = inspect.getsource(original)
                stats = {**GatewaySyncTiming.counters(),
                         "source_sha256": hashlib.sha256(source.encode()).hexdigest()}
                self.pool_stats.append(stats)
                self.originals.append(original)
                self.patches.enter_context(patch.object(pool, "_t4_batch_timing_owner", self, create=True))
                self.patches.enter_context(patch.object(pool, "_assign_requests_to_connections",
                                                       self._wrap(original, stats)))
            self.sched_before = self._sched()
            self.started_cpu, self.started_ns = time.thread_time_ns(), time.perf_counter_ns()
            self.callback = self._gc
            gc.callbacks.append(self.callback)
            self.active = True
        except BaseException:
            self.patches.close()
            raise
        return self

    def __exit__(self, *args):
        if not self.active:
            return
        self.active = False
        try:
            gc.callbacks.remove(self.callback)
        finally:
            self.patches.close()
        end, used = time.perf_counter_ns(), time.thread_time_ns() - self.started_cpu
        restored = all(pool._assign_requests_to_connections == original
                       and not hasattr(pool, "_t4_batch_timing_owner")
                       for pool, original in zip(self.pools, self.originals))
        self.result = {
            "scope": "main-thread-batch-pool-and-gc-overlap-not-additive",
            "valid": restored and self.callback not in gc.callbacks and not self.pending_gc and not self.gc_errors,
            "restored": restored, "started_ns": self.started_ns, "finished_ns": end,
            "wall_ns": end - self.started_ns, "main_cpu_ns": used,
            "main_thread_sched": sched_delta(self.sched_before, self._sched()),
            "schedstats_enabled": self.schedstats_enabled,
            "runqueue_reliable": self.schedstats_enabled is True,
            "foreign_pool_calls": self.foreign_pool_calls, "foreign_gc_callbacks": self.foreign_gc_callbacks,
            "pools": self.pool_stats, "gc": {**self.gc_stats, "errors": self.gc_errors,
                                              "truncated": self.gc_stats["calls"] > 128},
        }


class GatewayReceiveTap:
    """Install before connect: uvloop caches the protocol's bound read callback."""

    def __enter__(self):
        from anyio._backends._asyncio import StreamProtocol
        original = StreamProtocol.data_received
        self.source_sha256 = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
        self.current = None

        def observed(protocol, data):
            timing = self.current
            if timing is None or threading.get_ident() != timing.thread_id:
                return original(protocol, data)
            return timing._call("anyio_data_received", original, protocol, data, size=len(data))

        self.patch = patch.object(StreamProtocol, "data_received", observed)
        self.patch.start()
        return self

    def __exit__(self, *args):
        self.current = None
        self.patch.stop()


class GatewayBodyTiming:
    """Batch callback/write CPU; await spans are wall time, not task CPU.

    AnyIO callbacks may carry TLS records and have no request context. They are
    deliberately batch-only; never assign their bytes/CPU to the current task.
    """

    def __init__(self, *, receive_tap=None):
        self.receive_tap = receive_tap

    @staticmethod
    def counters():
        return {**GatewaySyncTiming.counters(), "size_bins": [0] * 7}

    def _add(self, name, count, wall, cpu=0, error=None):
        stats = self.owners[name]
        stats["calls"] += 1
        stats["bytes"] += count
        stats["wall_ns"] += wall
        if stats["cpu_ns"] is not None:
            stats["cpu_ns"] += cpu
        stats["max_wall_ns"] = max(stats["max_wall_ns"], wall)
        slot = next((i for i, bound in enumerate((0, 1024, 4096, 16384, 65536, 262144))
                     if count <= bound), 6)
        stats["size_bins"][slot] += 1
        if error:
            stats["errors"][error] = stats["errors"].get(error, 0) + 1

    def _call(self, name, original, *args, size=0):
        started, cpu = time.perf_counter_ns(), time.thread_time_ns()
        error = None
        try:
            return original(*args)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            used = time.thread_time_ns() - cpu
            elapsed = time.perf_counter_ns() - started
            self._add(name, size, elapsed, used, error)

    def _receive(self, original):
        def observed(protocol, data):
            if threading.get_ident() != self.thread_id:
                return original(protocol, data)
            return self._call("anyio_data_received", original, protocol, data, size=len(data))
        return observed

    def _send(self, original):
        owner = self

        class Transport:
            def __init__(self, transport, name):
                self.transport, self.name = transport, name

            def __getattr__(self, name):
                return getattr(self.transport, name)

            def write(self, data):
                return owner._call(self.name, self.transport.write, data, size=len(data))

        async def observed(cycle, message):
            if threading.get_ident() != self.thread_id or CURRENT_PROFILE.get() is None:
                return await original(cycle, message)
            is_body = message["type"] == "http.response.body"
            transport = cycle.transport
            proxy = Transport(transport, "transport_body_write" if is_body else "transport_header_write")
            cycle.transport = proxy
            started, error = time.perf_counter_ns(), None
            try:
                return await original(cycle, message)
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                cycle.transport = transport
                if is_body:
                    owner._add("asgi_body_send", len(message.get("body", b"")),
                               time.perf_counter_ns() - started, error=error)
        return observed

    def _drain(self, original):
        async def observed(flow):
            if threading.get_ident() != self.thread_id or CURRENT_PROFILE.get() is None:
                return await original(flow)
            started, error = time.perf_counter_ns(), None
            try:
                return await original(flow)
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                self._add("flow_drain", 0, time.perf_counter_ns() - started, error=error)
        return observed

    def __enter__(self):
        from anyio._backends._asyncio import StreamProtocol
        from uvicorn.protocols.http.flow_control import FlowControl
        self.thread_id = threading.get_ident()
        self.owners = {name: self.counters() for name in (
            "anyio_data_received", "transport_body_write", "transport_header_write",
            "asgi_body_send", "flow_drain")}
        for name in ("asgi_body_send", "flow_drain"):
            self.owners[name]["cpu_ns"] = None
        self.sources = {}
        self.patches = ExitStack()
        targets = [(FlowControl, "drain", self._drain)]
        receive_name = StreamProtocol.__module__ + ".StreamProtocol.data_received"
        if self.receive_tap is None:
            targets.insert(0, (StreamProtocol, "data_received", self._receive))
        else:
            if self.receive_tap.current is not None:
                raise RuntimeError("receive timing already active")
            self.sources[receive_name] = self.receive_tap.source_sha256
        try:
            for backend in ("h11_impl", "httptools_impl"):
                try:
                    module = importlib.import_module("uvicorn.protocols.http." + backend)
                except ModuleNotFoundError as exc:
                    if exc.name == "httptools" and backend == "httptools_impl":
                        continue
                    raise
                targets.append((module.RequestResponseCycle, "send", self._send))
            for cls, method, wrapper in targets:
                original = getattr(cls, method)
                name = cls.__module__ + "." + cls.__name__ + "." + method
                self.sources[name] = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
                self.patches.enter_context(patch.object(cls, method, wrapper(original)))
        except BaseException:
            self.patches.close()
            raise
        if self.receive_tap is not None:
            self.receive_tap.current = self
        self.started_ns, self.started_cpu = time.perf_counter_ns(), time.thread_time_ns()
        return self

    def __exit__(self, *args):
        if self.receive_tap is not None:
            self.receive_tap.current = None
        self.patches.close()
        self.result = {"scope": "batch-synchronous-cpu-and-overlapping-await-wall-not-network-delivery",
                       "receive_scope": "all-main-thread-anyio-callbacks-including-tls-framing",
                       "receive_installed_before_connect": self.receive_tap is not None,
                       "wall_ms": (time.perf_counter_ns() - self.started_ns) / 1e6,
                       "main_cpu_ms": (time.thread_time_ns() - self.started_cpu) / 1e6,
                       "source_sha256": self.sources,
                       "size_bin_upper_bytes": [0, 1024, 4096, 16384, 65536, 262144, None],
                       "owners": self.owners}


class GatewayCPUSampler:
    """Sample the interrupted Python stack by process CPU time, not wall time."""

    def __init__(self, *, interval=.005, capacity=4096):
        if not hasattr(signal, "SIGPROF") or threading.current_thread() is not threading.main_thread():
            raise RuntimeError("SIGPROF sampling requires the main thread on Linux")
        if not .001 <= interval <= .1 or not 2 <= capacity <= 8192:
            raise ValueError("bounded sampling required")
        self.interval = interval
        self.samples = deque(maxlen=capacity)

    def _handle(self, _signum, frame):
        stack = []
        try:
            while frame is not None and len(stack) < 12:
                stack.append({"file": Path(frame.f_code.co_filename).name,
                              "function": frame.f_code.co_name, "line": frame.f_lineno})
                frame = frame.f_back
        finally:
            del frame
        self.samples.append({"at_us": time.time_ns() // 1000, "stack": stack})

    def __enter__(self):
        self.previous_timer = signal.getitimer(signal.ITIMER_PROF)
        if self.previous_timer != (0.0, 0.0):
            raise RuntimeError("process CPU profiling timer already active")
        self.previous_handler = signal.getsignal(signal.SIGPROF)
        self.started_us = time.time_ns() // 1000
        self.started_cpu = time.process_time_ns()
        self.previous_thread_cpu = time.thread_time_ns()
        try:
            signal.signal(signal.SIGPROF, self._handle)
            signal.setitimer(signal.ITIMER_PROF, self.interval, self.interval)
        except BaseException:
            signal.setitimer(signal.ITIMER_PROF, *self.previous_timer)
            signal.signal(signal.SIGPROF, self.previous_handler)
            raise
        return self

    def __exit__(self, *args):
        finished_us = time.time_ns() // 1000
        process_cpu = time.process_time_ns() - self.started_cpu
        thread_cpu = time.thread_time_ns() - self.previous_thread_cpu
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, self.previous_handler)
        samples = [row for row in self.samples if self.started_us <= row["at_us"] <= finished_us]
        self.result = {"scope": "process-cpu-triggered-python-stack-not-profiling-proof",
                       "interval_ms": self.interval * 1000, "started_us": self.started_us,
                       "finished_us": finished_us, "wall_ms": (finished_us - self.started_us) / 1000,
                       "process_cpu_ms": process_cpu / 1e6, "main_thread_cpu_ms": thread_cpu / 1e6,
                       "sample_count": len(samples), "truncated": len(self.samples) == self.samples.maxlen,
                       "samples": samples}


class GatewayCPUProfile:
    """Main-thread CPU ownership only; instrumented rows are not speed evidence."""

    def __enter__(self):
        if sys.getprofile() is not None:
            raise RuntimeError("another profiler is active")
        self.profile = cProfile.Profile(timer=time.thread_time)
        self.started_cpu = time.thread_time()
        self.profile.enable()
        return self

    def __exit__(self, *args):
        self.profile.disable()
        measured_cpu_ms = (time.thread_time() - self.started_cpu) * 1000
        stats = pstats.Stats(self.profile).stats
        rows = [{"file": Path(file).name, "line": line, "function": name,
                 "calls": values[1], "self_cpu_ms": values[2] * 1000,
                 "cumulative_cpu_ms": values[3] * 1000}
                for (file, line, name), values in stats.items()]
        self.result = validate_cpu_profile(rows, measured_cpu_ms)
        self.profile.clear()


def validate_cpu_profile(rows, measured_cpu_ms):
    tolerance = max(2, measured_cpu_ms * .02)
    invalid = [r for r in rows if any(
        not math.isfinite(r[key]) or r[key] < 0 or r[key] > measured_cpu_ms + tolerance
        for key in ("self_cpu_ms", "cumulative_cpu_ms")
    )]
    return {"timer": "main-thread-cpu-not-wall", "functions": len(rows),
                       "valid": not invalid, "invalid_functions": len(invalid),
                       "measured_cpu_ms": measured_cpu_ms,
                       "self_cpu_ms": sum(row["self_cpu_ms"] for row in rows) if not invalid else None,
                       "top_self": sorted(rows, key=lambda r: r["self_cpu_ms"], reverse=True)[:40] if not invalid else [],
                       "top_cumulative": sorted(rows, key=lambda r: r["cumulative_cpu_ms"], reverse=True)[:40] if not invalid else []}


class FirstReadStream(httpx.AsyncByteStream):
    def __init__(self, stream, exchange, *, clock=time.perf_counter_ns):
        self.stream, self.exchange = stream, exchange
        self.clock = clock

    async def __aiter__(self):
        stats = self.exchange["body_io"] = {
            "read_calls": 0, "read_ns": 0, "read_max_ns": 0,
            "resume_ns": 0, "resume_max_ns": 0, "chunks": 0, "bytes": 0,
            "span_ns": 0, "eof": False,
        }
        iterator = self.stream.__aiter__()
        start = previous = self.clock()
        stats["started_us"] = time.time_ns() // 1000
        while True:
            before = self.clock()
            gap = before - previous
            stats["resume_ns"] += gap
            stats["resume_max_ns"] = max(stats["resume_max_ns"], gap)
            try:
                chunk = await anext(iterator)
            except StopAsyncIteration:
                stats["eof"] = True
                return
            except BaseException as exc:
                stats["read_error"] = type(exc).__name__
                raise
            finally:
                previous = self.clock()
                elapsed = previous - before
                stats["read_calls"] += 1
                stats["read_ns"] += elapsed
                if elapsed >= stats["read_max_ns"]:
                    stats["read_max_started_us"] = stats["started_us"] + (before - start) // 1000
                    stats["read_max_finished_us"] = stats["started_us"] + (previous - start) // 1000
                stats["read_max_ns"] = max(stats["read_max_ns"], elapsed)
                stats["span_ns"] = previous - start
                stats["finished_us"] = stats["started_us"] + (previous - start) // 1000
            stats["chunks"] += 1
            stats["bytes"] += len(chunk)
            if chunk:
                self.exchange["raw_last_us"] = time.time_ns() // 1000
            if chunk and "raw_first_us" not in self.exchange:
                self.exchange["raw_first_us"] = self.exchange["raw_last_us"]
                self.exchange["raw_first_size"] = len(chunk)
            yield chunk

    async def aclose(self):
        started = self.clock()
        try:
            await self.stream.aclose()
        finally:
            stats = self.exchange.setdefault("body_io", {})
            stats["close_ns"] = stats.get("close_ns", 0) + self.clock() - started
            stats["close_calls"] = stats.get("close_calls", 0) + 1


class GatewaySendTiming:
    """Gateway-only wall-await counters, including scheduling, never body capture."""

    def __init__(self, app, *, clock=time.perf_counter_ns):
        self.app, self.clock = app, clock

    async def __call__(self, scope, receive, send):
        event = CURRENT_PROFILE.get()
        if event is None:
            return await self.app(scope, receive, send)
        stats = event["body_send"] = {"calls": 0, "bytes": 0, "wait_ns": 0, "max_ns": 0}

        async def observed(message):
            if message["type"] != "http.response.body":
                return await send(message)
            started = self.clock()
            try:
                await send(message)
                stats["bytes"] += len(message.get("body", b""))
            except BaseException as exc:
                stats["error"] = type(exc).__name__
                raise
            finally:
                elapsed = self.clock() - started
                stats["calls"] += 1
                stats["wait_ns"] += elapsed
                stats["max_ns"] = max(stats["max_ns"], elapsed)

        await self.app(scope, receive, observed)


def pool_snapshot(client, request):
    # Diagnostic metadata only. No URL, cookies, headers, request repr or body.
    pool = getattr(client._transport_for_url(request.url), "_pool", None)
    if pool is None:
        return {"available": False}
    connections = list(pool.connections)
    return {"available": True, "connections": len(connections),
            "idle": sum(c.is_idle() for c in connections),
            "queued_or_active": len(getattr(pool, "_requests", ())),
            "max_connections": getattr(pool, "_max_connections", None)}


class GatewayTimeline:
    def __init__(self):
        self.events = deque(maxlen=2048)
        self.pools = set()
        self.assignments = deque(maxlen=8192)
        self.loop_lag = deque(maxlen=8192)

    async def watch_loop(self):
        while True:
            due = time.time_ns() // 1000 + 5000
            await asyncio.sleep(.005)
            self.loop_lag.append({"due_us": due, "resumed_us": time.time_ns() // 1000})

    def instrument_pool(self, pool):
        if pool is None or pool in self.pools:
            return
        self.pools.add(pool)
        original_assign = pool._assign_requests_to_connections
        original_create = pool.create_connection
        identity = hashlib.sha256(inspect.getsource(type(pool)._assign_requests_to_connections).encode()).hexdigest()
        next_id = 0

        def observe_connection(connection):
            nonlocal next_id
            if getattr(connection, "_t4_probe", False):
                return connection
            connection._t4_probe = True
            next_id += 1
            connection_id = next_id
            original_handle = connection.handle_async_request

            async def handle(request):
                exchange = request.extensions.get("t4_diagnostic_exchange")
                if exchange is None:
                    return await original_handle(request)
                attempt = {"connection": connection_id, "started_us": time.time_ns() // 1000}
                attempts = exchange.setdefault("attempts", [])
                if len(attempts) < 64:
                    attempts.append(attempt)
                else:
                    exchange["attempts_truncated"] = True
                try:
                    return await original_handle(request)
                except BaseException as exc:
                    attempt["error"] = type(exc).__name__
                    raise
                finally:
                    attempt["finished_us"] = time.time_ns() // 1000

            connection.handle_async_request = handle
            return connection

        def created(origin):
            return observe_connection(original_create(origin))

        def assigned():
            event = CURRENT_PROFILE.get()
            if event is None or not event.get("exchanges"):
                return original_assign()
            exchange = event["exchanges"][-1]
            exchange["assign_source_sha256"] = identity
            exchange["assign_policy"] = "reserved_http1" if getattr(pool, "reserve_http1", False) else "original"
            started_us, started = time.time_ns() // 1000, time.perf_counter_ns()
            try:
                return original_assign()
            finally:
                elapsed_us = (time.perf_counter_ns() - started) // 1000
                record = {"trace": event["trace"], "started_us": started_us, "duration_us": elapsed_us}
                self.assignments.append(record)
                exchange["assign_calls"] = exchange.get("assign_calls", 0) + 1
                exchange["assign_total_us"] = exchange.get("assign_total_us", 0) + elapsed_us
                exchange["assign_max_us"] = max(exchange.get("assign_max_us", 0), elapsed_us)

        for connection in pool.connections:
            observe_connection(connection)
        pool.create_connection = created
        pool._assign_requests_to_connections = assigned

    def install(self, app):
        previous_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def observed_lifespan(application):
            async with previous_lifespan(application):
                task = asyncio.create_task(self.watch_loop())
                try:
                    yield
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        app.router.lifespan_context = observed_lifespan
        app.add_middleware(GatewaySendTiming)
        app.add_middleware(ProfileMiddleware, events=self.events)
        bridge = app.state.playback
        original = bridge.open_media_async

        async def observed_open(asset, method, headers, *, redirect=False):
            event = CURRENT_PROFILE.get()
            if event is None:
                return await original(asset, method, headers, redirect=redirect)
            event.update(open_start_us=time.time_ns() // 1000,
                         asset_kind=asset.kind, prefix_known=asset.prefix is not None,
                         standard=asset.standard, local_transport=asset.local_transport)
            try:
                return await original(asset, method, headers, redirect=redirect)
            finally:
                event["open_finished_us"] = time.time_ns() // 1000

        bridge.open_media_async = observed_open
        for label, client in (("public", bridge.media_client), ("local", bridge._local_media_client)):
            async def requested(request, *, client=client, label=label):
                event = CURRENT_PROFILE.get()
                if event is None:
                    return
                exchanges = event.setdefault("exchanges", [])
                if len(exchanges) >= 32:
                    event["exchanges_truncated"] = True
                    return
                exchange = {"started_us": time.time_ns() // 1000, "transport_us": {},
                            "client": label, "pool": pool_snapshot(client, request)}
                exchanges.append(exchange)
                self.instrument_pool(getattr(client._transport_for_url(request.url), "_pool", None))
                request.extensions["t4_diagnostic_exchange"] = exchange

                async def trace(name, info):
                    if name in TRANSPORT_EVENTS:
                        exchange["transport_us"][name] = time.time_ns() // 1000

                request.extensions["trace"] = trace
                # Synthetic correlation ends here; never forward it to a real media host.

            async def responded(response):
                event = CURRENT_PROFILE.get()
                if event is None or event.get("exchanges_truncated") or not event.get("exchanges"):
                    return
                exchange = event["exchanges"][-1]
                exchange.update(headers_us=time.time_ns() // 1000,
                                status=response.status_code, http_version=response.http_version)
                response.stream = FirstReadStream(response.stream, exchange)

            client.event_hooks["request"].append(requested)
            client.event_hooks["response"].append(responded)

    async def collect(self, samples, child, mode):
        # Control traffic runs after measurement and must not reuse media/idle sockets.
        # No retries: a fresh-connection failure still fails the diagnostic gate.
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=2,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        ) as control:
            return await self._collect(samples, child, mode, control)

    async def _collect(self, samples, child, mode, control):
        traces = {s["trace"] for s in samples}
        deadline = time.monotonic() + 2
        while True:
            gateway = [e for e in self.events if e["trace"] in traces]
            if mode == "python":
                response = await control.get(child.url + "/__profile")
                response.raise_for_status()
                events = response.json()["events"]
            else:
                events = []
                for line in child.log.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("event") == "relay.closed":
                        events.append(event)
            relay = [e for e in events if e.get("trace", e.get("client_trace")) in traces]
            if len(gateway) >= len(traces) and len(relay) >= len(traces) or time.monotonic() >= deadline:
                result = join_timelines(samples, relay, gateway)
                for row in result:
                    gateway = row.get("gateway", {})
                    if not gateway.get("exchanges"):
                        continue
                    exchange = gateway["exchanges"][-1]
                    body = exchange.get("body_io", {})
                    row["gateway_loop_body"] = lag_summary(
                        self.loop_lag, body.get("started_us"), body.get("finished_us"),
                    )
                    row["gateway_loop_longest_read"] = lag_summary(
                        self.loop_lag, body.get("read_max_started_us"), body.get("read_max_finished_us"),
                    )
                    start = exchange["started_us"]
                    end = exchange.get("raw_first_us", start)
                    overlapping = [v for v in self.assignments
                                   if v["started_us"] < end and v["started_us"] + v["duration_us"] > start]
                    row["gateway_assign_overlap_ms"] = sum(
                        max(0, min(v["started_us"] + v["duration_us"], end) - max(v["started_us"], start))
                        for v in overlapping
                    ) / 1000
                    row["gateway_assign_overlap_calls"] = len(overlapping)
                return result
            await asyncio.sleep(.01)


def lag_summary(events, start, end):
    """Ticker lateness in the same interval, not attribution of every read wait."""
    if start is None or end is None or end < start or not events:
        return {"available": False}
    overlapping = [e for e in events if e["due_us"] <= end and e["resumed_us"] >= start]
    gaps = [max(0, min(e["resumed_us"], end) - max(e["due_us"], start)) for e in overlapping]
    return {"available": True, "covered": events[0]["due_us"] <= start and events[-1]["resumed_us"] >= end,
            "ticks": len(overlapping), "interval_ms": (end - start) / 1000,
            "delayed_ms": sum(gaps) / 1000, "max_lag_ms": max(gaps, default=0) / 1000,
            "unobserved_end_ms": max(0, end - events[-1]["resumed_us"]) / 1000}


def join_timelines(samples, relay_events, gateway_events):
    """Join the exact request, not differences between unrelated percentile values."""
    def unique(events):
        indexed, duplicates = {}, set()
        for event in events:
            key = event.get("trace", event.get("client_trace"))
            if key in indexed:
                duplicates.add(key)
            indexed[key] = event
        return {k: v for k, v in indexed.items() if k not in duplicates}

    relays, gateways = unique(relay_events), unique(gateway_events)
    joined = []
    for sample in samples:
        key = sample.get("trace")
        relay, gateway = relays.get(key), gateways.get(key)
        row = {"trace": key, "complete": False}
        joined.append(row)
        if relay is None or gateway is None or not sample.get("client_first_body_us"):
            row["reason"] = "missing_or_duplicate_trace"
            continue
        exchanges = gateway.get("exchanges", [])
        if not exchanges or gateway.get("exchanges_truncated"):
            row["reason"] = "missing_or_truncated_upstream"
            continue
        exchange = exchanges[-1]
        transport = exchange["transport_us"]
        sent = transport.get("http11.send_request_headers.complete",
                             transport.get("http2.send_request_headers.complete"))
        points = [sample.get("client_start_us"), relay.get("handler_us"),
                  relay.get("upstream_request_us"), gateway.get("handler_us"),
                  gateway.get("open_start_us"), exchanges[0].get("started_us"),
                  sent, exchange.get("headers_us"), exchange.get("raw_first_us"),
                  gateway.get("first_body_us"), relay.get("first_body_us"),
                  sample["client_first_body_us"]]
        names = ("client_to_relay", "relay_setup", "relay_to_gateway", "gateway_dispatch",
                 "gateway_open_setup", "gateway_acquire_connect_send", "upstream_header_wait",
                 "upstream_headers_to_raw_body", "gateway_body_prepare", "gateway_to_relay",
                 "relay_to_consumer")
        if any(p is None for p in points):
            row["reason"] = "missing_node"
        elif any(b < a for a, b in zip(points, points[1:])):
            row["reason"] = "clock_or_node_order"
        else:
            row["stages_ms"] = {name: (b - a) / 1000 for name, a, b in zip(names, points, points[1:])}
            row["total_ms"] = (points[-1] - points[0]) / 1000
            row["clock_error_ms"] = abs(row["total_ms"] - sample["ttfb_ms"])
            row["complete"] = row["clock_error_ms"] < 2
        row.update(relay=relay, gateway=gateway)
    return joined
