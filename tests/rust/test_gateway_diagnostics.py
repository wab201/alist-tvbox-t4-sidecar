import asyncio
from contextlib import nullcontext
import hashlib
import json
import socket
import ssl
import sys
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
import httpx
import pytest
import uvicorn

from scripts.benchmark_rust_selected_media import ProcessConsumer
from scripts.diagnose_rust_gateway import FirstReadStream, GatewayTimeline, GatewaySendTiming, GatewayCPUProfile, join_timelines, lag_summary, validate_cpu_profile
from scripts.diagnose_rust_gateway import GatewayBodyTiming, GatewayCPUSampler, GatewayReceiveTap, GatewayStackSampler, GatewaySyncTiming, parse_schedstat, sched_delta
from sidecar.gateway.media_response import MediaResponse
from sidecar.playback import MediaAsset, PlaybackBridge
from tests.rust.relay_support import Child, CURRENT_PROFILE, HEADERS, binary_path, experiment, pattern


def synthetic_timeline():
    sample = {"trace": "same", "client_start_us": 1000, "client_first_body_us": 12000, "ttfb_ms": 11}
    relay = {"client_trace": "same", "handler_us": 2000, "upstream_request_us": 3000,
             "first_body_us": 11000}
    gateway = {"trace": "same", "handler_us": 4000, "open_start_us": 5000,
               "first_body_us": 10000, "exchanges": [{
                   "started_us": 6000, "transport_us": {"http11.send_request_headers.complete": 7000},
                   "headers_us": 8000, "raw_first_us": 9000}]}
    return sample, relay, gateway


@pytest.mark.parametrize("text", ["1 2", "1 2 3 4", "-1 2 3", "broken"])
def test_schedstat_rejects_invalid_counters(text):
    with pytest.raises(ValueError):
        parse_schedstat(text)


def test_schedstat_deltas_preserve_missing_and_reset_states():
    assert sched_delta(parse_schedstat("10 20 3"), parse_schedstat("15 27 5")) == {
        "available": True, "run_ns": 5, "runqueue_ns": 7, "timeslices": 2}
    assert not sched_delta(None, parse_schedstat("0 0 0"))["available"]
    assert sched_delta(parse_schedstat("1 1 1"), parse_schedstat("0 0 0"))["counter_reset"]


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGPROF"), reason="Linux process CPU timer required")
def test_cpu_sampler_uses_cpu_timer_restores_state_and_bounds_samples():
    import signal
    def busy():
        deadline = time.process_time() + .04
        value = 0
        while time.process_time() < deadline:
            value += 1
        return value

    old_handler = signal.getsignal(signal.SIGPROF)
    old_timer = signal.getitimer(signal.ITIMER_PROF)
    sampler = GatewayCPUSampler(interval=.001, capacity=128)
    assert busy() > 0
    with sampler:
        busy()
    assert signal.getsignal(signal.SIGPROF) is old_handler
    assert signal.getitimer(signal.ITIMER_PROF) == old_timer
    assert not sampler.result["truncated"] and 2 <= sampler.result["sample_count"] <= 128
    assert sampler.result["process_cpu_ms"] >= 35 and sampler.result["wall_ms"] >= 30
    assert any(frame["function"] == "busy" for row in sampler.result["samples"] for frame in row["stack"])


@pytest.mark.parametrize("failed", [False, True])
def test_stack_sampler_is_bounded_redacted_and_always_stops(failed):
    def sampled_fixture(secret):
        time.sleep(.1)
        if failed:
            raise ValueError(secret)

    sampler = GatewayStackSampler(interval=.001, capacity=4)
    if failed:
        with pytest.raises(ValueError):
            with sampler:
                sampled_fixture("private-media-url")
    else:
        with sampler:
            sampled_fixture("private-media-url")
    assert not sampler.worker.is_alive()
    result = sampler.result
    assert 0 < result["sample_count"] <= 4 and result["truncated"]
    assert result["errors"] == 0 and result["observer_cpu_ms"] >= 0
    assert result["wall_ms"] >= 90 and result["max_sample_gap_ms"] > 0
    assert any(frame["function"] == "sampled_fixture" for row in result["samples"] for frame in row["stack"])
    assert "private-media-url" not in json.dumps(result)
    assert all(set(frame) == {"file", "function", "line"}
               for row in result["samples"] for frame in row["stack"])
    assert not any(t.name == "gateway-stack-observer" for t in threading.enumerate())


@pytest.mark.parametrize("failed", [False, True])
def test_sync_timing_keeps_returns_errors_trace_and_restores(monkeypatch, failed):
    import httpcore._synchronization as synchronization
    def read(self, length=1024):
        if length == -1:
            raise ssl.SSLWantReadError("private-media-url")
        return b"secret-bytes"

    monkeypatch.setattr(ssl.SSLObject, "read", read)
    original = synchronization.current_async_library
    event = {}
    token = CURRENT_PROFILE.set(event)
    timing = GatewaySyncTiming()
    async def detect():
        return synchronization.current_async_library()
    try:
        with timing:
            assert ssl.SSLObject.read(None) == b"secret-bytes"
            assert asyncio.run(detect()) == "asyncio"
            with pytest.raises(ssl.SSLWantReadError):
                ssl.SSLObject.read(None, -1)
            if failed:
                raise ValueError("private-media-url")
    except ValueError:
        assert failed
    finally:
        CURRENT_PROFILE.reset(token)
    assert ssl.SSLObject.read is read and synchronization.current_async_library is original
    owners = timing.result["owners"]
    assert owners["ssl_read"]["calls"] == 2 and owners["ssl_read"]["bytes"] == 12
    assert owners["ssl_read"]["errors"] == {"SSLWantReadError": 1}
    assert owners["async_library"]["calls"] == 1
    assert event["sync_work"] == owners
    assert all(row["wall_ns"] > 0 and row["cpu_ns"] >= 0 for row in owners.values())
    assert "private-media-url" not in json.dumps(timing.result)
    assert "secret-bytes" not in json.dumps(timing.result)


def test_async_backend_detection_avoids_repeated_missing_import_lookups(monkeypatch):
    import sniffio
    import httpcore._synchronization as synchronization
    import importlib._bootstrap_external as bootstrap
    assert sniffio.__version__

    async def check():
        assert synchronization.current_async_library() == "asyncio"
        def unexpected_lookup(*args):
            raise AssertionError("backend detection searched the filesystem")
        with monkeypatch.context() as patcher:
            patcher.setattr(bootstrap, "_path_stat", unexpected_lookup)
            for _ in range(100):
                assert synchronization.current_async_library() == "asyncio"
    asyncio.run(check())


@pytest.mark.parametrize("failure", [None, OSError, asyncio.CancelledError])
@pytest.mark.parametrize("sampled", [False, True])
def test_body_timing_preserves_transport_flow_context_and_errors(failure, sampled):
    from anyio._backends._asyncio import StreamProtocol
    from uvicorn.protocols.http.flow_control import FlowControl
    from uvicorn.protocols.http.h11_impl import RequestResponseCycle
    originals = (StreamProtocol.data_received, FlowControl.drain, RequestResponseCycle.send)

    async def check():
        writes, original_transport = [], SimpleNamespace(marker="same")
        original_transport.write = lambda data: writes.append(data)
        cycle = SimpleNamespace(transport=original_transport)
        flow = FlowControl(original_transport)
        flow.pause_writing()

        async def send(cycle, message):
            assert cycle.transport.marker == "same"
            if message["type"] == "http.response.body":
                asyncio.get_running_loop().call_later(.02, flow.resume_writing)
                await flow.drain()
                if failure:
                    raise failure("private-media-url")
                cycle.transport.write(message["body"])
            else:
                cycle.transport.write(b"private-header")
            return "unchanged"

        timing = GatewayBodyTiming()
        token = CURRENT_PROFILE.set({} if sampled else None)
        try:
            with timing:
                protocol = StreamProtocol()
                protocol.connection_made(SimpleNamespace(set_write_buffer_limits=lambda value: None))
                data = bytearray(b"private-media-bytes")
                protocol.data_received(data)
                assert protocol.read_queue.pop() == data and protocol.read_event.is_set()
                wrapped = timing._send(send)
                assert await wrapped(cycle, {"type": "http.response.start"}) == "unchanged"
                body = {"type": "http.response.body", "body": b"private-media-bytes"}
                if failure:
                    with pytest.raises(failure):
                        await wrapped(cycle, body)
                else:
                    assert await wrapped(cycle, body) == "unchanged"
                assert cycle.transport is original_transport
        finally:
            CURRENT_PROFILE.reset(token)
        assert (StreamProtocol.data_received, FlowControl.drain, RequestResponseCycle.send) == originals
        owners = timing.result["owners"]
        assert owners["anyio_data_received"]["calls"] == 1
        assert owners["anyio_data_received"]["bytes"] == len(data)
        assert owners["asgi_body_send"]["calls"] == int(sampled)
        assert owners["transport_body_write"]["calls"] == int(sampled and not failure)
        assert owners["transport_header_write"]["calls"] == int(sampled)
        assert owners["flow_drain"]["calls"] == int(sampled)
        assert owners["flow_drain"]["cpu_ns"] is None
        if sampled:
            assert owners["flow_drain"]["wall_ns"] >= 15_000_000
            assert owners["asgi_body_send"]["errors"] == ({failure.__name__: 1} if failure else {})
        assert len(writes) == (1 if failure else 2)
        encoded = json.dumps(timing.result)
        assert "private-media" not in encoded and "private-header" not in encoded
        assert all(len(s["size_bins"]) == 7 and sum(s["size_bins"]) == s["calls"] for s in owners.values())
    asyncio.run(check())


def test_body_timing_bounds_chunk_counters_and_restores_on_outer_error():
    from anyio._backends._asyncio import StreamProtocol
    original = StreamProtocol.data_received
    timing = GatewayBodyTiming()
    with pytest.raises(ValueError):
        with timing:
            for size in (0, 1, 1024, 1025, 4096, 4097, 16384, 16385, 65536, 65537, 262144, 262145):
                timing._call("transport_body_write", lambda: None, size=size)
            raise ValueError("private-media-url")
    stats = timing.result["owners"]["transport_body_write"]
    assert stats["size_bins"] == [1, 2, 2, 2, 2, 2, 1]
    assert stats["calls"] == 12 and StreamProtocol.data_received is original
    assert "private-media-url" not in json.dumps(timing.result)


@pytest.mark.parametrize("loop_kind", ["asyncio", "uvicorn"])
def test_body_receive_tap_captures_warmed_connection_and_stops_counting(loop_kind):
    import anyio
    from anyio._backends._asyncio import StreamProtocol
    original = StreamProtocol.data_received

    async def check():
        peers, done = [], asyncio.Event()

        async def echo(reader, writer):
            peers.append(True)
            try:
                while data := await reader.read(64):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                done.set()

        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        async with server:
            with GatewayReceiveTap() as tap:
                stream = await anyio.connect_tcp("127.0.0.1", server.sockets[0].getsockname()[1])
                async with stream:
                    await stream.send(b"warm")
                    assert await stream.receive(4) == b"warm"
                    timing = GatewayBodyTiming(receive_tap=tap)
                    with timing:
                        await stream.send(b"measured")
                        assert await stream.receive(8) == b"measured"
                    counter = timing.result["owners"]["anyio_data_received"]
                    assert counter["calls"] == 1 and counter["bytes"] == 8
                    assert timing.result["receive_installed_before_connect"]
                    await stream.send(b"off")
                    assert await stream.receive(3) == b"off"
                    assert counter["calls"] == 1 and tap.current is None
                await asyncio.wait_for(done.wait(), 1)
        assert len(peers) == 1 and StreamProtocol.data_received is original

    factory = uvicorn.Config("unused:app").get_loop_factory() if loop_kind == "uvicorn" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(check())


@pytest.mark.parametrize("failed", [False, True])
def test_cpu_profile_reports_owners_not_arguments_and_stops_on_failure(failed):
    def fixture(secret):
        total = 0
        # Require measured CPU, not an iteration count affected by JIT/clock granularity.
        deadline = time.thread_time() + .08
        while time.thread_time() < deadline:
            for i in range(10_000):
                total += i * i
        if failed:
            raise ValueError(secret)
        return total

    profile = GatewayCPUProfile()
    if failed:
        with pytest.raises(ValueError):
            with profile:
                fixture("private-media-address")
    else:
        with profile:
            assert fixture("private-media-address") > 0
    assert sys.getprofile() is None
    assert profile.result["timer"] == "main-thread-cpu-not-wall"
    assert profile.result["valid"]
    assert profile.result["self_cpu_ms"] > 0
    assert any(r["function"] == "fixture" and r["calls"] == 1 for r in profile.result["top_self"])
    encoded = json.dumps(profile.result)
    assert "private-media-address" not in encoded
    assert len(profile.result["top_self"]) <= 40
    assert len(profile.result["top_cumulative"]) <= 40


@pytest.mark.parametrize("self_ms,cumulative_ms", [(-1, 10), (10, -1), (1000, 1000), (float("nan"), 1), (1, float("inf"))])
def test_invalid_cpu_times_are_not_ranked_as_hotspots(self_ms, cumulative_ms):
    result = validate_cpu_profile([{"self_cpu_ms": self_ms, "cumulative_cpu_ms": cumulative_ms}], 100)
    assert not result["valid"] and result["invalid_functions"] == 1
    assert result["top_self"] == result["top_cumulative"] == []


def test_loop_lag_is_clipped_to_exact_interval_and_preserves_missing_coverage():
    events = [{"due_us": 0, "resumed_us": 2000}, {"due_us": 7000, "resumed_us": 8000},
              {"due_us": 13000, "resumed_us": 15000}]
    summary = lag_summary(events, 1000, 14000)
    assert summary["covered"] and summary["interval_ms"] == 13
    assert summary["delayed_ms"] == 3 and summary["max_lag_ms"] == 1
    assert summary["ticks"] == 3
    partial = lag_summary(events, 1000, 20000)
    assert not partial["covered"] and partial["unobserved_end_ms"] == 5
    assert not lag_summary([], 0, 1)["available"]
    assert not lag_summary(events, None, None)["available"]


def test_gateway_loop_ticker_is_bounded_and_stops_with_lifespan():
    async def check():
        probe, bridge = GatewayTimeline(), PlaybackBridge()
        app = FastAPI()
        app.state.playback = bridge
        probe.install(app)
        try:
            async with app.router.lifespan_context(app):
                await asyncio.sleep(.025)
                assert 0 < len(probe.loop_lag) <= probe.loop_lag.maxlen == 8192
            count = len(probe.loop_lag)
            await asyncio.sleep(.01)
            assert len(probe.loop_lag) == count
        finally:
            bridge.close()
            await bridge.aclose()
    asyncio.run(check())


def test_gateway_join_uses_one_trace_and_conserves_total():
    sample, relay, gateway = synthetic_timeline()
    row = join_timelines([sample], [relay], [gateway])[0]
    assert row["complete"] and sum(row["stages_ms"].values()) == row["total_ms"] == 11
    assert row["clock_error_ms"] == 0
    assert set(row["stages_ms"].values()) == {1}


@pytest.mark.parametrize("problem", ["duplicate", "missing", "wrong_trace", "clock", "truncated"])
def test_gateway_join_does_not_invent_missing_or_bad_nodes(problem):
    sample, relay, gateway = synthetic_timeline()
    gateways = [gateway]
    if problem == "duplicate":
        gateways.append(dict(gateway))
    elif problem == "missing":
        gateway["exchanges"][0].pop("raw_first_us")
    elif problem == "wrong_trace":
        gateway["trace"] = "other"
    elif problem == "clock":
        gateway["handler_us"] = 100
    else:
        gateway["exchanges_truncated"] = True
    assert not join_timelines([sample], [relay], gateways)[0]["complete"]


def test_raw_stream_observer_preserves_bytes_and_close():
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b""
            yield b"a"
            yield b"bc"

        async def aclose(self):
            self.closed = True

    async def check():
        original, event = Stream(), {}
        stream = FirstReadStream(original, event)
        assert b"".join([piece async for piece in stream]) == b"abc"
        await stream.aclose()
        assert original.closed and event["raw_first_size"] == 1 and event["raw_first_us"] > 0
        stats = event["body_io"]
        assert stats["eof"] and stats["bytes"] == 3 and stats["chunks"] == 3
        assert stats["read_calls"] == 4 and stats["close_calls"] == 1
        assert stats["read_ns"] + stats["resume_ns"] == stats["span_ns"]
    asyncio.run(check())


def test_read_and_processing_time_are_disjoint_without_per_chunk_records():
    class Clock:
        now = 0

        def __call__(self):
            return self.now

    async def check():
        clock, event = Clock(), {}

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(1000):
                    clock.now += 7
                    yield b"x"

            async def aclose(self):
                clock.now += 2

        stream = FirstReadStream(Stream(), event, clock=clock)
        async for chunk in stream:
            assert chunk == b"x"
            clock.now += 3
        await stream.aclose()
        stats = event["body_io"]
        assert stats["read_ns"] == 7000 and stats["resume_ns"] == 3000
        assert stats["span_ns"] == 10000 and stats["read_max_ns"] == 7
        assert stats["resume_max_ns"] == 3 and stats["close_ns"] == 2
        assert stats["chunks"] == stats["bytes"] == 1000
        assert not any(isinstance(v, (list, dict, bytes)) for v in stats.values())
    asyncio.run(check())


@pytest.mark.parametrize("error", [httpx.ReadError, asyncio.CancelledError])
def test_read_failure_is_propagated_and_close_preserved(error):
    async def check():
        event, closed = {}, []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"abc"
                raise error("fixture")

            async def aclose(self):
                closed.append(True)

        stream = FirstReadStream(Stream(), event)
        with pytest.raises(error):
            async for _ in stream:
                pass
        await stream.aclose()
        assert closed == [True] and event["body_io"]["read_error"] == error.__name__
        assert event["body_io"]["bytes"] == 3 and not event["body_io"]["eof"]
    asyncio.run(check())


@pytest.mark.parametrize("sampled", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_gateway_send_timer_measures_only_send_and_preserves_failures(sampled, failure):
    async def check():
        ticks = [0]
        messages = []
        event = {} if sampled else None
        token = CURRENT_PROFILE.set(event)

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            ticks[0] += 100
            await send({"type": "http.response.body", "body": b"abc", "more_body": False})

        async def send(message):
            ticks[0] += 7
            if failure and message["type"] == "http.response.body":
                raise OSError("fixture")
            messages.append(message)

        try:
            timed = GatewaySendTiming(app, clock=lambda: ticks[0])
            if failure:
                with pytest.raises(OSError):
                    await timed({}, None, send)
            else:
                await timed({}, None, send)
            if sampled:
                assert event["body_send"]["wait_ns"] == event["body_send"]["max_ns"] == 7
                assert event["body_send"]["bytes"] == (0 if failure else 3)
            assert len(messages) == (1 if failure else 2)
        finally:
            CURRENT_PROFILE.reset(token)
    asyncio.run(check())


@pytest.mark.skipif(not binary_path().is_file(), reason="release relay required")
@pytest.mark.parametrize("reserved", [False, True])
@pytest.mark.parametrize("source", ["file", "slow"])
def test_real_gateway_three_hop_timeline_and_probe_off(tmp_path, reserved, source, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    async def check(origin):
        size, concurrency = (262144, 16) if source == "slow" else (32768, 2)
        expected = hashlib.sha256(b"".join(
            pattern(offset, min(65536, size - offset)) for offset in range(0, size, 65536)
        )).hexdigest()
        bridge = PlaybackBridge()
        # Windows also discovers system proxies outside environment variables.
        await bridge.media_client.aclose()
        bridge.media_client = httpx.AsyncClient(trust_env=False, follow_redirects=True)
        bridge._assets["fixture"] = MediaAsset(
            None, origin.url + "/" + source, HEADERS, "application/octet-stream", "test", time.monotonic(),
            kind="segment", prefix=0, standard=True,
        )
        app = FastAPI()
        app.state.playback = bridge

        @app.api_route("/t4/media/{key}", methods=["GET", "HEAD"])
        async def media(key: str, request: Request):
            return MediaResponse(bridge, key, bridge.get(key), request)

        probe = GatewayTimeline()
        if reserved:
            from scripts.diagnose_rust_pool import install_reservation, set_reservation
            set_reservation(install_reservation(app), True)
        probe.install(app)
        consumer, children = ProcessConsumer(), []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            server = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="critical"))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                while not server.started:
                    await asyncio.sleep(.01)
                manifest = tmp_path / "gateway-assets.json"
                manifest.write_text(json.dumps({"real": {
                    "url": f"http://127.0.0.1:{listener.getsockname()[1]}/t4/media/fixture", "headers": {},
                }}))
                commands = {
                    "python": [sys.executable, "-X", "utf8", "-m", "tests.rust.relay_support",
                               "--serve", "python", "--assets", str(manifest), "--profile"],
                    "rust": [str(binary_path()), "--assets", str(manifest), "--trace"],
                }
                await consumer.start()
                async with httpx.AsyncClient(trust_env=False) as client:
                    for mode, command in commands.items():
                        child = await asyncio.to_thread(Child, command, tmp_path, mode)
                        children.append(child)
                        for on in (False, True):
                            timing = GatewayBodyTiming(receive_tap=receive_tap) if on else None
                            with timing if timing is not None else nullcontext():
                                batch = await consumer.batch(child.url + "/t4/media/real", concurrency, size, True,
                                                             trace_prefix=f"gw-{mode}" if on else None)
                            assert all(s["passed"] and s["sha256"] == expected
                                       for s in batch["samples"])
                            if on:
                                owners = timing.result["owners"]
                                assert owners["anyio_data_received"]["calls"] > 0
                                assert owners["transport_body_write"]["bytes"] == size * concurrency
                                assert owners["asgi_body_send"]["bytes"] == size * concurrency
                                assert owners["transport_header_write"]["calls"] == concurrency
                                assert owners["transport_body_write"]["cpu_ns"] >= 0
                                joined = await probe.collect(batch["samples"], child, mode)
                                assert len(joined) == concurrency and all(r["complete"] for r in joined), joined
                                assert all(r["gateway"]["asset_kind"] == "segment" for r in joined)
                                assert all(r["gateway"]["exchanges"][0]["assign_calls"] > 0 for r in joined)
                                assert all(r["gateway"]["exchanges"][0]["attempts"] for r in joined)
                                assert all(r["gateway_assign_overlap_ms"] >= 0 for r in joined)
                                for row in joined:
                                    raw = row["gateway"]["exchanges"][0]["body_io"]
                                    sent = row["gateway"]["body_send"]
                                    assert raw["bytes"] == sent["bytes"] == size
                                    assert raw["eof"] and raw["read_ns"] + raw["resume_ns"] == raw["span_ns"]
                                    assert sent["wait_ns"] >= sent["max_ns"] >= 0
                                    assert row["gateway_loop_body"]["available"]
                                    assert row["gateway_loop_longest_read"]["available"]
                                encoded = json.dumps(joined)
                                assert "fixture=cookie" not in encoded and "fixture-not-a-secret" not in encoded
                                assert "127.0.0.1" not in encoded
                        assert len(probe.events) == len(children) * concurrency
                    origin_events = (await client.get(origin.url + "/__profile")).json()["events"]
                    assert not any(e["trace"].startswith("gw-") for e in origin_events)
            finally:
                await consumer.close()
                for child in reversed(children):
                    await asyncio.to_thread(child.close)
                server.should_exit = True
                await serving
                bridge.close()
                await bridge.aclose()

    with experiment(profile=True) as instances, GatewayReceiveTap() as receive_tap:
        asyncio.run(check(instances["origin"]))


@pytest.mark.parametrize("failure", [False, True])
def test_profile_collection_uses_fresh_control_connections_without_retries(failure):
    async def check():
        peers, methods = [], []
        sample, relay, gateway = synthetic_timeline()
        probe = GatewayTimeline()
        probe.events.append(gateway)

        async def serve(reader, writer):
            try:
                while request := await reader.readuntil(b"\r\n\r\n"):
                    peers.append(writer.get_extra_info("peername")[1])
                    methods.append(request.split(b"\r\n", 1)[0])
                    if failure:
                        return
                    payload = json.dumps({"events": [] if len(peers) == 1 else [relay]}).encode()
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode()
                                 + b"\r\nContent-Type: application/json\r\n\r\n" + payload)
                    await writer.drain()
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        async with server:
            child = SimpleNamespace(url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            if failure:
                with pytest.raises(httpx.RemoteProtocolError):
                    await probe.collect([sample], child, "python")
                assert len(peers) == 1
            else:
                assert (await probe.collect([sample], child, "python"))[0]["complete"]
                assert len(peers) == 2 and len(set(peers)) == 2
            assert set(methods) == {b"GET /__profile HTTP/1.1"}
    asyncio.run(check())
