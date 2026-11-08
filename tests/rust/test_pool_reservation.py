import asyncio
from types import SimpleNamespace

import httpcore
import httpx
import pytest
from httpcore._async.connection_pool import AsyncPoolRequest

from scripts.diagnose_rust_pool import ReservedHTTP1Pool, install_reservation, set_reservation


class Connection:
    def __init__(self, origin, *, idle=True, expired=False, closed=False):
        self.origin, self.idle, self.expired, self.closed = origin, idle, expired, closed

    def is_idle(self):
        return self.idle

    def is_available(self):
        return self.idle and not self.closed

    def is_closed(self):
        return self.closed

    def has_expired(self):
        return self.expired

    def can_handle_request(self, origin):
        return self.origin == origin


def request(host=b"one.test"):
    return AsyncPoolRequest(httpcore.Request("GET", b"http://" + host + b"/file"))


def pool(size=16, keepalive=16):
    result = ReservedHTTP1Pool(max_connections=size, max_keepalive_connections=keepalive)
    result.create_connection = Connection
    result.reserve_http1 = True
    return result


@pytest.mark.parametrize("reserved", [False, True])
def test_reproduce_original_collision_and_prevent_it_across_assign_calls(reserved):
    async def check():
        p = pool()
        p.reserve_http1 = reserved
        first = request()
        p._connections = [Connection(first.request.url.origin) for _ in range(16)]
        # The real state lock yields before a reserved idle connection becomes ACTIVE.
        for _ in range(16):
            p._requests.append(request())
            assert not p._assign_requests_to_connections()
        assert len({r.connection for r in p._requests}) == (16 if reserved else 1)
        assert len(p.connections) == 16
    asyncio.run(check())


def test_queue_capacity_release_and_preserve_reserved_idle():
    async def check():
        p = pool(2, 0)
        p._requests = [request() for _ in range(3)]
        p._assign_requests_to_connections()
        assert len(p.connections) == 2 and p._requests[-1].is_queued()
        assert not p._assign_requests_to_connections()
        first = p._requests.pop(0).connection
        closed = p._assign_requests_to_connections()
        assert closed == [first]
        assert len({r.connection for r in p._requests}) == 2
        assert not any(r.is_queued() for r in p._requests)
    asyncio.run(check())


def test_mixed_origins_evict_only_unreserved_idle():
    async def check():
        p = pool(2)
        p._requests = [request()]
        p._assign_requests_to_connections()
        reserved = p._requests[0].connection
        idle = Connection(reserved.origin)
        p._connections.append(idle)
        p._requests.append(request(b"two.test"))
        assert p._assign_requests_to_connections() == [idle]
        assert reserved in p.connections and len(p.connections) == 2
        assert p._requests[1].connection.origin != reserved.origin
    asyncio.run(check())


def test_expired_and_closed_removed_but_active_not_evicted():
    async def check():
        p = pool(4, 1)
        origin = request().request.url.origin
        dead = [Connection(origin, expired=True), Connection(origin, closed=True)]
        active = Connection(origin, idle=False)
        p._connections = [*dead, active, Connection(origin)]
        assert p._assign_requests_to_connections() == dead
        assert active in p.connections and len(p.connections) == 2
    asyncio.run(check())


def test_install_is_local_empty_pool_only_and_retains_settings():
    async def check():
        async with httpx.AsyncClient(trust_env=False) as public, httpx.AsyncClient(trust_env=False) as local:
            app = SimpleNamespace(state=SimpleNamespace(playback=SimpleNamespace(
                media_client=public, _local_media_client=local)))
            old = public._transport._pool
            pools = install_reservation(app)
            assert type(old) is httpcore.AsyncConnectionPool and not old.connections
            assert public._transport._pool is pools[0]
            for key in ("_ssl_context", "_network_backend", "_max_connections",
                        "_keepalive_expiry", "_max_keepalive_connections", "_socket_options"):
                assert getattr(pools[0], key) == getattr(old, key)
            set_reservation(pools, True)
            assert all(p.reserve_http1 for p in pools)
            pools[0]._requests.append(request())
            with pytest.raises(AssertionError, match="all streams close"):
                set_reservation(pools, False)
            pools[0]._requests.clear()
    asyncio.run(check())


@pytest.mark.parametrize("bad", ["http2", "proxy_mount", "live"])
def test_install_rejects_unsupported_pool_without_partial_replacement(bad):
    async def check():
        async with httpx.AsyncClient(trust_env=False) as public, httpx.AsyncClient(trust_env=False) as local:
            app = SimpleNamespace(state=SimpleNamespace(playback=SimpleNamespace(
                media_client=public, _local_media_client=local)))
            old = public._transport._pool
            other = local._transport._pool
            if bad == "http2":
                other._http2 = True
            elif bad == "proxy_mount":
                local._mounts["test"] = local._transport
            else:
                other._requests.append(request())
            with pytest.raises(AssertionError):
                install_reservation(app)
            assert public._transport._pool is old
            other._requests.clear()
            local._mounts.clear()
    asyncio.run(check())


def test_actual_pool_timeout_cancellation_release_and_reuse():
    async def check():
        started = asyncio.Event()
        release = asyncio.Event()

        async def origin(reader, writer):
            try:
                while True:
                    await reader.readuntil(b"\r\n\r\n")
                    started.set()
                    await release.wait()
                    writer.write(b"HTTP/1.1 206 Partial Content\r\nContent-Length: 3\r\n"
                                 b"Content-Range: bytes 0-2/3\r\n\r\nabc")
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(origin, "127.0.0.1", 0)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/file"
        transport = httpx.AsyncHTTPTransport()
        p = ReservedHTTP1Pool(max_connections=1, max_keepalive_connections=1)
        p.reserve_http1 = True
        transport._pool = p
        try:
            async with httpx.AsyncClient(transport=transport, timeout=2) as client:
                active = asyncio.create_task(client.get(url))
                await asyncio.wait_for(started.wait(), 2)
                with pytest.raises(httpx.PoolTimeout):
                    await client.get(url, timeout=httpx.Timeout(2, pool=.03))
                queued = asyncio.create_task(client.get(url))
                await asyncio.sleep(.01)
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued
                assert len(p._requests) == 1
                release.set()
                response = await active
                assert response.content == b"abc" and response.status_code == 206
                assert not p._requests
                connection = p.connections[0]
                assert (await client.get(url)).content == b"abc"
                assert p.connections == [connection] and not p._requests
        finally:
            release.set()
            server.close()
            await server.wait_closed()
    asyncio.run(check())
