import asyncio
from contextlib import contextmanager
import re
import socket
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
import httpx
import pytest
import uvicorn

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.playback import MediaAsset


@contextmanager
def serving(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(
        app, log_level="critical", access_log=False,
        timeout_graceful_shutdown=2, lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
            time.sleep(.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive(), "test server leaked a thread"


def registered(app, url, *, kind="binary"):
    if kind == "proxy":
        return "/t4/proxy/" + app.state.playback._proxies.register(
            url, "socket-fixture", "socket-fixture",
        )
    return "/t4/media/" + app.state.playback._put(MediaAsset(
        None, url, {"Referer": "https://drive.example"},
        "application/octet-stream", "socket-fixture", time.monotonic(), kind,
        local_transport=kind == "at_proxy",
    ), ".bin")


def wait_for(predicate, seconds=1):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return bool(predicate())


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
@pytest.mark.parametrize("kind", ["binary", "hls", "segment", "proxy", "at_proxy"])
@pytest.mark.parametrize("phase", ["headers", "body"])
def test_real_disconnect_cancels_blocked_upstream_before_timeout(tmp_path, phase, kind, spec):
    entered, released = threading.Event(), threading.Event()
    upstream_app = FastAPI()

    @upstream_app.get("/stall")
    @upstream_app.get("/proxy")
    async def stall(request: Request):
        if phase == "headers":
            entered.set()
            while (await request.receive())["type"] != "http.disconnect":
                pass
            released.set()
            return {}

        async def chunks():
            try:
                yield b"x" * 16384
                entered.set()
                await asyncio.Event().wait()
            finally:
                released.set()

        return StreamingResponse(chunks(), media_type="application/octet-stream")

    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())

    async def forced_asgi(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "asgi": {**scope["asgi"], "spec_version": spec}}
        await app(scope, receive, send)

    with serving(upstream_app) as upstream, serving(forced_asgi) as gateway:
        path = registered(app, upstream + ("/proxy" if kind == "proxy" else "/stall"), kind=kind)
        client = socket.create_connection(("127.0.0.1", int(gateway.rsplit(":", 1)[1])))
        client.settimeout(3)
        try:
            client.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            assert entered.wait(3)
            if phase == "body" and kind != "hls":
                received = b""
                while b"x" * 1024 not in received:
                    received += client.recv(65536)
        finally:
            client.shutdown(socket.SHUT_RDWR)
            client.close()
        assert released.wait(1), "client left but the blocked upstream remained open"
        stage = "proxy.closed" if kind == "proxy" else "media.closed"
        assert wait_for(lambda: any(
            event["stage"] == stage for event in app.state.traces.events()
        )), "media lifecycle did not finish after disconnect"
        closed = [event for event in app.state.traces.events() if event["stage"] == stage]
        assert len(closed) == 1 and closed[0]["reason"] == "client_disconnect"
        assert httpx.get(gateway + "/t4/health", timeout=2).status_code == 200


def test_disconnecting_during_segment_inspection_closes_probe(tmp_path):
    entered, released = threading.Event(), threading.Event()
    upstream_app = FastAPI()

    @upstream_app.get("/segment")
    async def segment():
        async def chunks():
            try:
                yield b"\x89PNG\r\n\x1a\n"
                entered.set()
                await asyncio.Event().wait()
            finally:
                released.set()
        return StreamingResponse(chunks())

    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with serving(upstream_app) as upstream, serving(app) as gateway:
        path = registered(app, upstream + "/segment", kind="segment")
        with socket.create_connection(("127.0.0.1", int(gateway.rsplit(":", 1)[1]))) as client:
            client.sendall((
                f"GET {path} HTTP/1.1\r\nHost: localhost\r\nRange: bytes=0-99\r\n\r\n"
            ).encode())
            assert entered.wait(3)
            client.shutdown(socket.SHUT_RDWR)
        assert released.wait(1)
        assert wait_for(lambda: any(
            event["stage"] == "media.closed" for event in app.state.traces.events()
        ))


@pytest.mark.parametrize("kind", ["binary", "at_proxy"])
def test_real_large_file_if_range_and_repeated_seek_preserve_bytes(tmp_path, kind):
    total = 5 * 1024 ** 3
    etag = '"generation-2"'
    modified = "Wed, 28 Oct 2026 00:00:00 GMT"
    upstream_app = FastAPI()
    seen = []

    def sample(offset, size):
        return bytes((offset + n) % 251 for n in range(size))

    @upstream_app.api_route("/large", methods=["GET", "HEAD"])
    async def large(request: Request):
        assert request.headers["referer"] == "https://drive.example"
        assert request.headers["accept-encoding"] == "identity"
        seen.append((request.method, request.headers.get("range"), request.headers.get("if-range")))
        headers = {"ETag": etag, "Last-Modified": modified, "Accept-Ranges": "bytes"}
        requested = request.headers.get("range", "")
        begin, end, status = 0, total - 1, 200
        if requested and request.headers.get("if-range", etag) in {etag, modified}:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            assert match
            if match[1]:
                begin = int(match[1])
                end = min(int(match[2]), end) if match[2] else end
            else:
                begin = max(0, total - int(match[2]))
            if begin >= total or end < begin:
                return Response(status_code=416, headers={
                    **headers, "Content-Range": f"bytes */{total}", "Content-Length": "0",
                })
            status = 206
            headers["Content-Range"] = f"bytes {begin}-{end}/{total}"
        headers["Content-Length"] = str(end - begin + 1)
        if request.method == "HEAD":
            return Response(status_code=status, headers=headers)

        async def chunks():
            for offset in range(begin, end + 1, 65536):
                yield sample(offset, min(65536, end - offset + 1))
                await asyncio.sleep(0)
        return StreamingResponse(chunks(), status_code=status, headers=headers)

    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    app.state.playback._local_media_client = httpx.AsyncClient(
        trust_env=False, timeout=2, limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
    )
    with serving(upstream_app) as upstream, serving(app) as gateway, httpx.Client(timeout=3) as client:
        target = gateway + registered(app, upstream + "/large", kind=kind)
        head = client.head(target)
        assert head.status_code == 200 and head.headers["Content-Length"] == str(total)
        assert head.content == b"" and head.headers["ETag"] == etag
        # A single upstream pool slot must survive repeated close/reopen/seek.
        for _ in range(12):
            for offset in (0, total // 2, total - 8192):
                response = client.get(target, headers={
                    "Range": f"bytes={offset}-{offset + 8191}", "If-Range": etag,
                })
                assert response.status_code == 206
                assert response.content == sample(offset, 8192)
                assert response.headers["Content-Range"] == f"bytes {offset}-{offset + 8191}/{total}"
                assert response.headers["ETag"] == etag
        for validator in (etag, modified):
            response = client.get(target, headers={"Range": "bytes=-8192", "If-Range": validator})
            assert response.status_code == 206 and response.content == sample(total - 8192, 8192)
        with client.stream("GET", target, headers={"Range": f"bytes={total - 8192}-"}) as response:
            assert response.status_code == 206 and response.read() == sample(total - 8192, 8192)
        response = client.get(target, headers={"Range": f"bytes={total}-"})
        assert response.status_code == 416 and response.headers["Content-Range"] == f"bytes */{total}"
        # A stale validator must be an upstream 200, not a locally fabricated 206.
        with client.stream("GET", target, headers={
            "Range": "bytes=100-199", "If-Range": '"stale"',
        }) as response:
            assert response.status_code == 200
            assert "Content-Range" not in response.headers
            assert response.headers["Content-Length"] == str(total)
            assert next(response.iter_bytes(8192)) == sample(0, 8192)
        assert len(seen) == 42


@pytest.mark.parametrize("phase", ["headers", "body", "short"])
@pytest.mark.parametrize("kind", ["binary", "at_proxy"])
def test_upstream_timeout_or_short_body_is_logged_not_completed(tmp_path, phase, kind):
    released = threading.Event()
    upstream_app = FastAPI()

    @upstream_app.get("/failure")
    async def failure(request: Request):
        if phase == "headers":
            while (await request.receive())["type"] != "http.disconnect":
                pass
            released.set()
            return {}

        async def chunks():
            try:
                yield b"x" * 16384
                if phase == "body":
                    await asyncio.Event().wait()
            finally:
                released.set()
        return StreamingResponse(chunks(), headers={"Content-Length": "32768"})

    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    app.state.playback._local_media_client = httpx.AsyncClient(
        trust_env=False, timeout=httpx.Timeout(.2, connect=1),
    )
    with serving(upstream_app) as upstream, serving(app) as gateway, httpx.Client(timeout=3) as client:
        target = gateway + registered(app, upstream + "/failure", kind=kind)
        if phase == "headers":
            assert client.get(target).status_code == 502
        else:
            with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
                client.get(target)
        assert released.wait(1)
        assert wait_for(lambda: any(
            event["stage"] == "media.closed" for event in app.state.traces.events()
        ))
        events = app.state.traces.events()
        closed = next(event for event in events if event["stage"] == "media.closed")
        assert closed["reason"] == "upstream_error"
        stage = "media.failed" if phase == "headers" else "media.stream_failed"
        failed = next(event for event in events if event["stage"] == stage)
        if phase == "short":
            assert failed["error"] in {"RemoteProtocolError", "ReadError"}
        else:
            assert failed["error"] == "ReadTimeout"
        assert client.get(gateway + "/t4/health").status_code == 200


@pytest.mark.parametrize("status", [403, 404, 410])
def test_expired_signed_media_is_forwarded_without_blind_replay(tmp_path, status):
    upstream_app = FastAPI()
    calls = []

    @upstream_app.get("/signed")
    async def signed():
        calls.append(True)
        return Response(b"expired", status_code=status)

    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with serving(upstream_app) as upstream, serving(app) as gateway:
        target = gateway + registered(app, upstream + "/signed")
        response = httpx.get(target, timeout=3)
        assert response.status_code == status and response.content == b"expired"
        assert len(calls) == 1
