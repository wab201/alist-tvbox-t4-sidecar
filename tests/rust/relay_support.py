from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import gzip
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

from Crypto.Cipher import AES
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 ** 2
ETAG = '"fixture-generation-2"'
MODIFIED = "Wed, 28 Oct 2026 00:00:00 GMT"
HEADERS = {
    "Referer": "https://fixture.example",
    "Cookie": "fixture=cookie",
    "Authorization": "Bearer fixture-not-a-secret",
    "User-Agent": "T4-relay-fixture",
}
PATTERN = bytes(range(251)) * 263
KEY = bytes(range(16))
CIPHERTEXT = AES.new(KEY, AES.MODE_CBC, bytes(16)).encrypt(b"\x47" * 16384)
PROFILE_HEADER = "x-t4-relay-client-trace"
CURRENT_PROFILE: ContextVar[dict | None] = ContextVar("relay_profile", default=None)


class ProfileMiddleware:
    """Fixture-only, bounded per-request timeline; never captures URLs or auth headers."""

    def __init__(self, app, events):
        self.app, self.events = app, events

    async def __call__(self, scope, receive, send):
        trace = dict(scope.get("headers", [])).get(PROFILE_HEADER.encode(), b"")
        if scope["type"] != "http" or not re.fullmatch(rb"[A-Za-z0-9_.:-]{1,96}", trace):
            return await self.app(scope, receive, send)
        event = {
            "trace": trace.decode("ascii"), "handler_us": time.time_ns() // 1000,
            "peer_port": (scope.get("client") or [None, None])[1],
            "bytes": 0, "chunks": 0,
        }
        token = CURRENT_PROFILE.set(event)

        async def observed_send(message):
            first_body = False
            if message["type"] == "http.response.start":
                event["response_headers_us"] = time.time_ns() // 1000
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    first_body = "first_body_us" not in event
                    event.setdefault("first_body_us", time.time_ns() // 1000)
                    event.setdefault("first_body_size", len(body))
                    event["max_chunk_size"] = max(event.get("max_chunk_size", 0), len(body))
                    event["bytes"] += len(body)
                    event["chunks"] += 1
            await send(message)
            if first_body:
                event["first_body_sent_us"] = time.time_ns() // 1000

        try:
            await self.app(scope, receive, observed_send)
        finally:
            event["closed_us"] = time.time_ns() // 1000
            self.events.append(event)
            CURRENT_PROFILE.reset(token)


def configure_profile(app):
    events = deque(maxlen=20000)
    app.state.profile_lag = deque(maxlen=20000)

    @app.get("/__profile")
    async def profile():
        return {"events": list(events), "loop_lag": list(app.state.profile_lag)}

    app.add_middleware(ProfileMiddleware, events=events)


class ConnectionTraceMiddleware:
    """Carry only a synthetic ID; no body wrappers or event-loop sampling."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        trace = dict(scope.get("headers", [])).get(PROFILE_HEADER.encode(), b"")
        if scope["type"] != "http" or not re.fullmatch(rb"[A-Za-z0-9_.:-]{1,96}", trace):
            return await self.app(scope, receive, send)
        token = CURRENT_PROFILE.set({"trace": trace.decode("ascii")})
        try:
            await self.app(scope, receive, send)
        finally:
            CURRENT_PROFILE.reset(token)


def pattern(offset: int, size: int) -> bytes:
    assert 0 <= size <= 65536
    start = offset % 251
    return PATTERN[start:start + size]


def binary_path() -> Path:
    configured = os.environ.get("T4_RUST_RELAY_BIN")
    if configured:
        return Path(configured).resolve()
    name = "t4-media-relay.exe" if os.name == "nt" else "t4-media-relay"
    return ROOT / "rust" / "media-relay" / "target" / "release" / name


class Child:
    def __init__(self, command: list[str], directory: Path, name: str, *, cpus=None,
                 log_read_delay=0):
        self.log = directory / f"{name}.log"
        self.output = self.log.open("w", encoding="utf-8")
        if cpus is not None:
            if sys.platform != "linux" or not cpus:
                self.output.close()
                raise ValueError("launch affinity requires Linux and a nonempty CPU set")
            command = [sys.executable, "-c",
                       "import os,sys; os.sched_setaffinity(0,{!r}); "
                       "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)".format(set(cpus)),
                       *command]
        self.log_stop = threading.Event()
        self.log_reader = None
        self.process = subprocess.Popen(
            command, cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if log_read_delay else self.output,
            stderr=subprocess.STDOUT,
        )
        if log_read_delay:
            def drain_log():
                with self.process.stdout as stream:
                    while line := stream.readline(16384):
                        self.output.write(line.decode("utf-8", "replace"))
                        self.output.flush()
                        self.log_stop.wait(log_read_delay)
            self.log_reader = threading.Thread(target=drain_log, daemon=True)
            self.log_reader.start()
        self.url = ""
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"{name} exited: {self.log.read_text(encoding='utf-8')[-3000:]}")
                for line in self.log.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("event") == "ready":
                        self.url = "http://" + event["listen"]
                        return
                time.sleep(.02)
            raise RuntimeError(f"{name} did not start")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.log_stop.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(5)
        if self.log_reader is not None:
            self.log_reader.join(5)
            if self.log_reader.is_alive():
                raise RuntimeError("diagnostic log reader did not stop")
        self.output.close()


@contextmanager
def experiment(*, read_timeout_ms=25000, trace=False, profile=False, fair_source=False,
               legacy_source=False, hedge_delay_ms=None, hedge_limit=2,
               approve_all_assets=False, yield_after_first_body=False, delivery_budget_kib=None,
               upstream_read_kib=None, startup_fairness=False, cpu_layout=None,
               connections=False, relay_args=(), log_read_delay=0):
    executable = binary_path()
    if not executable.is_file():
        raise FileNotFoundError(f"build release relay first: {executable}")
    children = []
    with tempfile.TemporaryDirectory(prefix="t4-relay-poc-") as temporary:
        directory = Path(temporary)
        try:
            profile_args = ["--profile"] if profile else []
            if connections:
                profile_args.append("--connections")
            origin = Child(
                [sys.executable, "-X", "utf8", "-m", "tests.rust.relay_support",
                 "--serve", "origin", *profile_args, *(["--fair-source"] if fair_source else []),
                 *(["--legacy-source"] if legacy_source else [])],
                directory, "origin", cpus=(cpu_layout or {}).get("origin"),
            )
            children.append(origin)
            # Both transports consume the same synthetic headers and opaque-key mapping.
            targets = {
                "file": "/file", "large": f"/file?size={5 * 1024 ** 3}",
                "chunked": "/chunked", "encoded": "/encoded",
                "redirect": "/redirect", "cross": "/cross-redirect",
                "key": "/key", "encrypted": "/encrypted",
                "manifest": "/manifest", "404": "/status/404",
                "403": "/status/403", "429": "/status/429", "500": "/status/500",
                "stall-headers": "/stall/headers", "stall-body": "/stall/body",
                "truncated": "/truncated", "slow": "/slow",
                "ignore-range": "/ignore-range", "loop": "/loop",
                "signed": "/folder/space%20file.mkv?signature=a%2FB%2b&part=02&empty=&part=01",
            }
            for case in ("headers", "body", "backup-error", "backup-empty", "primary-error", "both-error",
                         "both-slow", "fast", "truncated", "shared", "gated-body",
                         "body-no-etag", "body-weak-etag", "body-changed-etag",
                         "body-changed-range"):
                targets["hedge-" + case] = "/hedge/" + case
            targets["hedge-large"] = "/hedge/headers?size=5368709120"
            # Bind and release only to obtain a definitely unused local failure target.
            with socket.socket() as unavailable:
                unavailable.bind(("127.0.0.1", 0))
                unreachable = f"http://127.0.0.1:{unavailable.getsockname()[1]}/unavailable"
            assets = {key: {"url": origin.url + path, "headers": HEADERS}
                      for key, path in targets.items()}
            assets["unavailable"] = {"url": unreachable, "headers": HEADERS}
            for key in assets:
                if key.startswith("hedge-") or approve_all_assets:
                    assets[key]["hedge"] = True
            manifest = directory / "assets.json"
            manifest.write_text(json.dumps(assets), encoding="utf-8")
            python = Child([
                sys.executable, "-X", "utf8", "-m", "tests.rust.relay_support",
                "--serve", "python", "--assets", str(manifest), *profile_args,
            ], directory, "python", cpus=(cpu_layout or {}).get("python"))
            children.append(python)
            rust_command = [
                str(executable), "--assets", str(manifest),
                "--read-timeout-ms", str(read_timeout_ms),
            ]
            if trace or profile:
                rust_command.append("--trace")
            if yield_after_first_body:
                rust_command.append("--yield-after-first-body")
            if delivery_budget_kib is not None:
                rust_command += ["--delivery-budget-kib", str(delivery_budget_kib)]
            if upstream_read_kib is not None:
                rust_command += ["--upstream-read-kib", str(upstream_read_kib)]
            if startup_fairness:
                rust_command.append("--startup-fairness")
            if hedge_delay_ms is not None:
                rust_command += [
                    "--hedge-delay-ms", str(hedge_delay_ms), "--hedge-limit", str(hedge_limit),
                ]
            rust = Child([*rust_command, *relay_args], directory, "rust",
                         cpus=(cpu_layout or {}).get("rust"), log_read_delay=log_read_delay)
            children.append(rust)
            with httpx.Client(trust_env=False, timeout=5) as client:
                for child in children:
                    assert client.get(child.url + "/health").status_code == 200
            yield {"origin": origin, "python": python, "rust": rust, "directory": directory}
        finally:
            for child in reversed(children):
                child.close()


def origin_app(*, profile=False, fair=False, legacy=False, connections=False) -> FastAPI:
    app = FastAPI()
    active = 0
    cancelled = 0
    generated_bytes = 0
    seen = deque(maxlen=100)
    hedge_attempts = {}
    hedge_header_active = 0
    hedge_header_peak = 0
    hedge_gate_active = 0
    shared_gate = asyncio.Lock()
    body_gate = asyncio.Event()
    if profile:
        configure_profile(app)

    @app.get("/health")
    async def health():
        return {
            "active": active, "cancelled": cancelled, "seen": list(seen),
            "generated_bytes": generated_bytes,
            "hedge_attempts": dict(hedge_attempts),
            "hedge_header_active": hedge_header_active, "hedge_header_peak": hedge_header_peak,
            "hedge_gate_active": hedge_gate_active,
        }

    @app.post("/__release_body")
    async def release_body():
        body_gate.set()
        return {"released": True}

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def source(path: str, request: Request):
        nonlocal active, cancelled, hedge_header_active, hedge_header_peak, hedge_gate_active
        seen.append({
            **({"trace": request.headers.get(PROFILE_HEADER),
                "peer_port": (request.scope.get("client") or [None, None])[1]}
               if connections else {}),
            "path": path, "method": request.method,
            "raw_path": request.scope["raw_path"].decode("ascii"),
            "query": request.scope["query_string"].decode("ascii"),
            "range": request.headers.get("range"),
            "if-range": request.headers.get("if-range"),
            "referer": request.headers.get("referer"),
            "cookie": request.headers.get("cookie"),
            "authorization": request.headers.get("authorization"),
            "accept-encoding": request.headers.get("accept-encoding"),
            "user-agent": request.headers.get("user-agent"),
        })
        if path.startswith("status/"):
            status = int(path.split("/")[1])
            return Response(b"fixture-error", status_code=status,
                            headers={"Retry-After": "17"} if status == 429 else None)
        body_delay = 0
        truncate_before_body = False
        if path.startswith("hedge/"):
            case = path.split("/")[1]
            # Each distinct seek range has its own deterministic slow first attempt.
            identity = path + ":" + request.headers.get("range", "")
            attempt = hedge_attempts.get(identity, 0) + 1
            hedge_attempts[identity] = attempt
            if case == "backup-error" and attempt > 1:
                return Response(b"fixture-error", status_code=403)
            if case == "backup-empty" and attempt > 1:
                return Response(status_code=204)
            if case == "both-error":
                await asyncio.sleep(.12 if attempt == 1 else .01)
                return Response(b"fixture-error", status_code=500 if attempt == 1 else 403)
            if case == "primary-error" and attempt == 1:
                await asyncio.sleep(.12)
                return Response(b"fixture-error", status_code=500)
            if case == "truncated" and attempt == 1:
                body_delay, truncate_before_body = .12, True
            if case in {"headers", "backup-error", "backup-empty", "both-slow"} and (
                attempt == 1 or case == "both-slow"
            ):
                hedge_header_active += 1
                hedge_header_peak = max(hedge_header_peak, hedge_header_active)
                try:
                    deadline = time.monotonic() + .3
                    while time.monotonic() < deadline:
                        if await request.is_disconnected():
                            return Response()
                        await asyncio.sleep(.005)
                finally:
                    hedge_header_active -= 1
            if (case == "body" or case.startswith("body-")) and attempt == 1:
                body_delay = .3
            if case == "shared":
                hedge_gate_active += 1
                try:
                    async with shared_gate:
                        await asyncio.sleep(.02)
                finally:
                    hedge_gate_active -= 1
        if path in {"redirect", "cross-redirect", "loop"}:
            target = "/cookie-file"
            if path == "cross-redirect":
                target = f"http://localhost:{request.url.port}/cross-file"
            elif path == "loop":
                target = "/loop"
            return Response(status_code=302, headers={
                "Location": target, "Set-Cookie": "origin_session=fixture; Path=/",
            })
        if path == "cookie-file" and "origin_session=fixture" not in request.headers.get("cookie", ""):
            return Response(b"cookie missing", status_code=403)
        if path == "cross-file" and request.headers.get("authorization"):
            return Response(b"credential forwarded across origins", status_code=403)
        if path in {"encoded", "key", "encrypted", "manifest"}:
            content, mime = {
                "encoded": (gzip.compress(b"fixture-not-decoded" * 100, mtime=0), "application/octet-stream"),
                "key": (KEY, "application/octet-stream"),
                "encrypted": (CIPHERTEXT, "video/mp2t"),
                "manifest": (b"#EXTM3U\n#EXTINF:5,\nsegment.ts\n#EXT-X-ENDLIST\n",
                             "application/vnd.apple.mpegurl"),
            }[path]
            headers = {"Content-Length": str(len(content)), "Content-Type": mime}
            if path == "encoded":
                headers["Content-Encoding"] = "gzip"
            return Response(b"" if request.method == "HEAD" else content, headers=headers)
        if path == "stall/headers":
            active += 1
            try:
                while (await request.receive())["type"] != "http.disconnect":
                    pass
            finally:
                active -= 1
                cancelled += 1
            return Response()

        total = int(request.query_params.get("size", 64 * MIB))
        start, end, status = 0, total - 1, 200
        headers = {
            "ETag": ETAG, "Last-Modified": MODIFIED, "Accept-Ranges": "bytes",
            "Content-Type": "video/x-matroska",
        }
        if path == "hedge/body-no-etag":
            headers.pop("ETag")
        elif path == "hedge/body-weak-etag":
            headers["ETag"] = "W/" + ETAG
        elif path == "hedge/body-changed-etag" and attempt > 1:
            headers["ETag"] = '"fixture-other-generation"'
        requested = request.headers.get("range")
        if (
            requested and path != "ignore-range"
            and request.headers.get("if-range", ETAG) in {ETAG, MODIFIED}
        ):
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if match is None or not any(match.groups()):
                return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), end) if match[2] else end
            else:
                start = max(0, total - int(match[2]))
            if start >= total or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            if path == "hedge/body-changed-range" and attempt > 1:
                headers["Content-Range"] = f"bytes {start + 1}-{end + 1}/{total}"
        if path != "chunked":
            headers["Content-Length"] = str(end - start + 1)
        if request.method == "HEAD":
            return Response(status_code=status, headers=headers)

        async def chunks():
            nonlocal active, cancelled, generated_bytes
            active += 1
            finished = False
            try:
                if path == "hedge/gated-body" or (
                    path in {"hedge/body-changed-etag", "hedge/body-changed-range"}
                    and attempt == 1
                ):
                    await body_gate.wait()
                if body_delay:
                    await asyncio.sleep(body_delay)
                if truncate_before_body:
                    return
                for offset in range(start, end + 1, 65536):
                    # ASGI send may become a no-op after disconnect. Bound fixture
                    # work even then; never generate the rest of a discarded 5 GiB body.
                    if not legacy and (offset - start) % (16 * 65536) == 0:
                        await asyncio.sleep(0)
                        if await request.is_disconnected():
                            return
                    chunk = pattern(offset, min(65536, end - offset + 1))
                    generated_bytes += len(chunk)
                    yield chunk
                    if fair:
                        await asyncio.sleep(0)
                    if path == "stall/body":
                        await asyncio.Event().wait()
                    if path == "truncated":
                        return
                    if path == "slow":
                        await asyncio.sleep(.02)
                finished = True
            finally:
                active -= 1
                if not finished:
                    cancelled += 1

        return StreamingResponse(chunks(), headers=headers, status_code=status)

    return app


def python_app(manifest: Path, *, profile=False, connections=False, configure_bridge=None) -> FastAPI:
    from sidecar.gateway.media_response import MediaResponse
    from sidecar.playback import MediaAsset, PlaybackBridge

    bridge = PlaybackBridge()
    for key, config in json.loads(manifest.read_text(encoding="utf-8")).items():
        bridge._assets[key] = MediaAsset(
            None, config["url"], config["headers"], config.get("mime", "application/octet-stream"),
            "relay-poc", time.monotonic(),
        )
    if configure_bridge is not None:
        configure_bridge(bridge)

    @asynccontextmanager
    async def lifespan(app):
        yield
        bridge.close()
        await bridge.media_client.aclose()
        await bridge._local_media_client.aclose()

    app = FastAPI(lifespan=lifespan)
    if connections and not profile:
        app.add_middleware(ConnectionTraceMiddleware)

        async def connection_trace(request):
            if (event := CURRENT_PROFILE.get()) is not None:
                request.headers[PROFILE_HEADER] = event["trace"]

        for client in (bridge.media_client, bridge._local_media_client):
            client.event_hooks["request"].append(connection_trace)
    if profile:
        configure_profile(app)

        async def upstream_request(request):
            if (event := CURRENT_PROFILE.get()) is not None:
                request.headers[PROFILE_HEADER] = event["trace"]
                started = time.time_ns() // 1000
                event.setdefault("upstream_request_us", started)
                exchange = {"started_us": started, "transport_us": {}}
                event.setdefault("upstream_exchanges", []).append(exchange)
                event["transport_us"] = exchange["transport_us"]

                async def trace(name, info):
                    if name in {
                        "connection.connect_tcp.started", "connection.connect_tcp.complete",
                        "connection.start_tls.started", "connection.start_tls.complete",
                        "http11.send_request_headers.started", "http11.send_request_headers.complete",
                        "http11.receive_response_headers.started", "http11.receive_response_headers.complete",
                    }:
                        exchange["transport_us"][name] = time.time_ns() // 1000

                request.extensions["trace"] = trace

        async def upstream_response(response):
            if (event := CURRENT_PROFILE.get()) is not None:
                received = time.time_ns() // 1000
                event["upstream_headers_us"] = received
                if exchanges := event.get("upstream_exchanges"):
                    exchanges[-1].update(headers_us=received, status=response.status_code,
                                         http_version=response.http_version,
                                         connection_close="close" in [
                                             v.strip().lower() for v in response.headers.get(
                                                 "connection", "",
                                             ).split(",")
                                         ])

        for client in (bridge.media_client, bridge._local_media_client):
            client.event_hooks["request"].append(upstream_request)
            client.event_hooks["response"].append(upstream_response)

    @app.get("/health")
    async def health():
        return {"baseline": "existing-MediaResponse"}

    @app.api_route("/t4/media/{key}", methods=["GET", "HEAD"])
    async def media(key: str, request: Request):
        asset = bridge.get(key)
        if asset is None:
            raise HTTPException(404)
        return MediaResponse(bridge, key, asset, request)

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", choices=["origin", "python"], required=True)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--connections", action="store_true")
    parser.add_argument("--fair-source", action="store_true")
    parser.add_argument("--legacy-source", action="store_true")
    args = parser.parse_args()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    app = origin_app(
        profile=args.profile, fair=args.fair_source, legacy=args.legacy_source,
        connections=args.connections,
    ) if args.serve == "origin" else python_app(
        args.assets, profile=args.profile, connections=args.connections,
    )
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def ready(application):
        async with previous_lifespan(application):
            async def sample_lag():
                while True:
                    due = time.time_ns() // 1000 + 5000
                    await asyncio.sleep(.005)
                    resumed = time.time_ns() // 1000
                    application.state.profile_lag.append({
                        "due_us": due, "resumed_us": resumed,
                        "lag_ms": max(0, (resumed - due) / 1000),
                    })

            ticker = asyncio.create_task(sample_lag()) if args.profile else None
            try:
                print(json.dumps({"event": "ready", "listen": f"127.0.0.1:{sock.getsockname()[1]}"}),
                      flush=True)
                yield
            finally:
                if ticker is not None:
                    ticker.cancel()
                    await asyncio.gather(ticker, return_exceptions=True)

    app.router.lifespan_context = ready
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical"))
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()


if __name__ == "__main__":
    main()
