import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import socket
import ssl
from types import SimpleNamespace

from anyio._backends._asyncio import SocketStream
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpcore
from httpcore._backends.anyio import AnyIOStream
import httpx
import pytest

from scripts.diagnose_rust_socket import SocketLookupStream, install_socket_lookup


def test_live_socket_identity_readability_and_closed_socket_are_not_cached():
    left, right = socket.socketpair()
    other, other_peer = socket.socketpair()
    try:
        holder = {"socket": left}
        stream = SocketStream(SimpleNamespace(get_extra_info=lambda name: holder[name]), None)
        counts = {"socket_fastpath_calls": 0}
        original, candidate = AnyIOStream(stream), SocketLookupStream(stream, counts)
        for state in ("idle", "readable", "replacement", "closed"):
            if state == "readable":
                right.sendall(b"x")
            elif state == "replacement":
                holder["socket"] = other
            elif state == "closed":
                other.close()
            assert candidate.get_extra_info("socket") is original.get_extra_info("socket")
            assert candidate.get_extra_info("is_readable") == original.get_extra_info("is_readable")
        assert counts["socket_fastpath_calls"] == 8
        assert candidate.get_extra_info("unknown") is None
    finally:
        for sock in (left, right, other, other_peer):
            sock.close()


def test_unsupported_stream_and_address_attributes_keep_upstream_semantics():
    class Unknown:
        def extra(self, attribute, default=None):
            return (attribute, default)
    counts = {"socket_fastpath_calls": 0}
    stream = Unknown()
    for info in ("socket", "ssl_object", "server_addr", "client_addr", "unknown"):
        assert SocketLookupStream(stream, counts).get_extra_info(info) == AnyIOStream(stream).get_extra_info(info)
    assert counts["socket_fastpath_calls"] == 0


def test_real_gateway_fastpath_keeps_range_bytes_and_records_actual_use(tmp_path):
    from scripts.diagnose_rust_replay import replay_gateway

    async def check():
        body = (b"\x47" + b"\x00" * 187) * 512
        handlers = set()

        async def handle(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                while True:
                    await reader.readuntil(b"\r\n\r\n")
                    headers = (f"HTTP/1.1 206 Partial Content\r\nContent-Length: {len(body)}\r\n"
                               f"Content-Range: bytes 0-{len(body)-1}/{len(body)}\r\n"
                               "Content-Type: video/mp2t\r\n\r\n").encode()
                    writer.write(headers + body)
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        manifest = tmp_path / "gateway.json"
        manifest.write_text(json.dumps({"real": {"url": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/bytes",
                                                "headers": {}}}), encoding="utf-8")
        try:
            for fast in (False, True):
                app = replay_gateway(manifest, "16k", socket_fastpath=fast)
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                        for _ in range(2):
                            response = await client.get("/t4/media/real", headers={"Range": f"bytes=0-{len(body)-1}"})
                            assert response.status_code == 206 and response.content == body
                            assert response.headers["content-range"] == f"bytes 0-{len(body)-1}/{len(body)}"
                        metrics = (await client.get("/__replay_metrics")).json()
                        assert metrics["yielded_bytes"] == 2 * len(body)
                        assert (metrics["socket_fastpath_calls"] > 0) == fast
                        assert bool((await client.get("/__replay_socket_identity")).json()) == fast
        finally:
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*handlers), 3)
    asyncio.run(check())


@pytest.mark.parametrize("problem", ["version", "active", "connected", "http2", "proxy"])
def test_install_rejects_unsupported_pools_without_partial_change(monkeypatch, problem):
    pools = [httpcore.AsyncConnectionPool(), httpcore.AsyncConnectionPool()]
    original = pools[0]._network_backend
    if problem == "version":
        monkeypatch.setattr(httpcore, "__version__", "unknown")
    elif problem == "active":
        pools[1]._requests.append(object())
    elif problem == "connected":
        pools[1]._connections.append(object())
    elif problem == "http2":
        pools[1]._http2 = True
    else:
        pools[1]._proxy = object()
    with pytest.raises(RuntimeError):
        install_socket_lookup(pools, {})
    assert pools[0]._network_backend is original


@pytest.fixture
def tls_contexts(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_file, key_file)
    client = ssl.create_default_context(cafile=str(cert_file))
    return server, client


@pytest.mark.parametrize("tls", [False, True])
@pytest.mark.parametrize("candidate", [False, True])
def test_real_reuse_peer_close_expiry_timeout_cancel_and_tls(tls, candidate, tls_contexts):
    async def check():
        server_tls, client_tls = tls_contexts
        accepted, handlers = [], set()
        entered, release = asyncio.Event(), asyncio.Event()

        async def handle(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            accepted.append(writer)
            try:
                while True:
                    request = await reader.readuntil(b"\r\n\r\n")
                    if request.startswith(b"GET /hold "):
                        entered.set()
                        await release.wait()
                    closing = request.startswith(b"GET /close ")
                    # TLS close_notify can already be inside SSL's buffer, not the raw socket.
                    close_header = b"Connection: close\r\n" if tls and closing else b""
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n" + close_header + b"\r\nabc")
                    await writer.drain()
                    if closing:
                        return
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, ssl.SSLError):
                    pass
                handlers.discard(task)

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_tls if tls else None)
        try:
            url = f"{'https' if tls else 'http'}://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with httpx.AsyncClient(trust_env=False, timeout=2, verify=client_tls,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1,
                                       keepalive_expiry=30)) as client:
                counts = {"socket_fastpath_calls": 0}
                pool = client._transport._pool
                if candidate:
                    install_socket_lookup([pool], counts)
                assert (await client.get(url)).content == b"abc"
                assert (await client.get(url)).content == b"abc"
                assert len(accepted) == 1
                network = pool.connections[0]._connection._network_stream
                assert (type(network) is AnyIOStream) == (tls or not candidate)
                assert (await client.get(url + "/close")).content == b"abc"
                await asyncio.sleep(.03)
                assert (await client.get(url)).content == b"abc"
                assert len(accepted) == 2
                pool.connections[0]._connection._expire_at = 0
                assert (await client.get(url)).content == b"abc"
                assert len(accepted) == 3
                active = asyncio.create_task(client.get(url + "/hold"))
                await asyncio.wait_for(entered.wait(), 2)
                try:
                    with pytest.raises(httpx.PoolTimeout):
                        await client.get(url, timeout=httpx.Timeout(2, pool=.03))
                    queued = asyncio.create_task(client.get(url))
                    await asyncio.sleep(.01)
                    queued.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await queued
                    active.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await active
                finally:
                    release.set()
                    if not active.done():
                        await active
                assert (await client.get(url)).content == b"abc"
                entered.clear()
                release.clear()
                with pytest.raises(httpx.ReadTimeout):
                    await client.get(url + "/hold", timeout=.05)
                release.set()
                assert (await client.get(url)).content == b"abc"
                assert not pool._requests
                assert (counts["socket_fastpath_calls"] > 0) == (candidate and not tls)
            if tls:
                async with httpx.AsyncClient(trust_env=False, timeout=2) as untrusted:
                    install_socket_lookup([untrusted._transport._pool], {})
                    with pytest.raises(httpx.ConnectError):
                        await untrusted.get(url)
        finally:
            release.set()
            server.close()
            await server.wait_closed()
            for writer in accepted:
                writer.close()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*handlers), 3)
    asyncio.run(check())
