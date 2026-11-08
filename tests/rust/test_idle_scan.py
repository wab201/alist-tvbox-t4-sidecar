import asyncio
from contextlib import nullcontext
import itertools
from types import SimpleNamespace

import httpcore
import httpx
import pytest
from httpcore._async.connection_pool import AsyncPoolRequest

from scripts.diagnose_rust_gateway import BatchRuntimeTiming
from scripts.diagnose_rust_idle_scan import ElideIdleScan, candidate_method


class Connection:
    def __init__(self, origin, name, state="idle"):
        self.origin, self.name, self.state = origin, name, state
        self.idle_calls = 0

    def is_idle(self):
        self.idle_calls += 1
        return self.state == "idle"

    def is_available(self):
        return self.state == "idle"

    def is_closed(self):
        return self.state == "closed"

    def has_expired(self):
        return self.state == "expired"

    def can_handle_request(self, origin):
        return self.origin == origin


def test_single_expression_removal_preserves_assignment_eviction_and_existing_collision():
    async def check():
        for states, keepalive in itertools.product(
                itertools.product(("idle", "active", "closed", "expired"), repeat=3), (0, 1, 4)):
            results = []
            for candidate in (False, True):
                pool = httpcore.AsyncConnectionPool(max_connections=4, max_keepalive_connections=keepalive)
                with ElideIdleScan([pool]) if candidate else nullcontext():
                    requests = [AsyncPoolRequest(httpcore.Request("GET", host)) for host in
                                ("http://one.test/a", "http://one.test/b", "http://two.test/c")]
                    inventory = [Connection(requests[0].request.url.origin, str(i), state)
                                 for i, state in enumerate(states)]
                    pool._connections = list(inventory)

                    def create(origin):
                        connection = Connection(origin, str(len(inventory)))
                        inventory.append(connection)
                        return connection

                    pool.create_connection = create
                    pool._requests = requests
                    closed = pool._assign_requests_to_connections()
                    results.append(([c.name for c in pool.connections], [c.name for c in closed],
                                    [r.connection.name if r.connection is not None else None for r in requests]))
                pool._requests.clear()
            assert results[0] == results[1], (states, keepalive, results)
    asyncio.run(check())


def test_single_expression_removal_reduces_idle_calls_without_global_patch():
    original = httpcore.AsyncConnectionPool._assign_requests_to_connections
    pool = httpcore.AsyncConnectionPool(max_connections=16, max_keepalive_connections=16)
    connection = Connection(httpcore.Origin(b"http", b"test", 80), "idle")
    pool._connections = [connection] * 16
    assert pool._assign_requests_to_connections() == []
    before = connection.idle_calls
    connection.idle_calls = 0
    with ElideIdleScan([pool]) as scan:
        with BatchRuntimeTiming([pool]) as timing:
            assert pool._assign_requests_to_connections() == []
        assert timing.result["valid"]
        assert timing.result["pools"][0]["source_sha256"] == scan.identity["candidate_sha256"]
        assert scan.identity["expression_replacements"] == 1
    assert before == 272 and connection.idle_calls == 16
    assert pool._assign_requests_to_connections.__func__ is original
    assert httpcore.AsyncConnectionPool._assign_requests_to_connections is original
    assert "_assign_requests_to_connections" not in pool.__dict__


@pytest.mark.parametrize("bad", ["version", "live", "http2", "proxy", "override"])
def test_idle_scan_rejects_unsupported_state_before_any_install(monkeypatch, bad):
    first, second = httpcore.AsyncConnectionPool(), httpcore.AsyncConnectionPool()
    if bad == "version":
        monkeypatch.setattr(httpcore, "__version__", "unsupported")
    elif bad == "live":
        second._requests.append(SimpleNamespace())
    elif bad == "http2":
        second._http2 = True
    elif bad == "proxy":
        second._proxy = object()
    else:
        second._assign_requests_to_connections = lambda: []
    original = first._assign_requests_to_connections
    with pytest.raises(RuntimeError):
        with ElideIdleScan([first, second]):
            pass
    assert first._assign_requests_to_connections == original
    assert "_assign_requests_to_connections" not in first.__dict__


def test_idle_scan_restores_after_outer_cancellation():
    pool = httpcore.AsyncConnectionPool()
    with pytest.raises(asyncio.CancelledError):
        with ElideIdleScan([pool]):
            raise asyncio.CancelledError()
    assert "_assign_requests_to_connections" not in pool.__dict__


def test_real_pool_timeout_cancellation_release_and_reuse_with_scan_elided():
    async def check():
        started, release = asyncio.Event(), asyncio.Event()
        handlers = set()

        async def origin(reader, writer):
            handlers.add(asyncio.current_task())
            try:
                while True:
                    await reader.readuntil(b"\r\n\r\n")
                    started.set()
                    await release.wait()
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc")
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(asyncio.current_task())

        server = await asyncio.start_server(origin, "127.0.0.1", 0)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2,
                                        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)) as client:
                pool = client._transport._pool
                with ElideIdleScan([pool]):
                    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
                    active = asyncio.create_task(client.get(url))
                    try:
                        await asyncio.wait_for(started.wait(), 2)
                        with pytest.raises(httpx.PoolTimeout):
                            await client.get(url, timeout=httpx.Timeout(2, pool=.03))
                        queued = asyncio.create_task(client.get(url))
                        await asyncio.sleep(.01)
                        queued.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await queued
                    finally:
                        release.set()
                        first = await active
                    assert first.content == b"abc"
                    assert (await client.get(url)).content == b"abc"
                    assert not pool._requests and len(pool.connections) == 1
                    assert pool.connections[0].is_idle()
        finally:
            release.set()
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*handlers), 2)
    asyncio.run(check())
