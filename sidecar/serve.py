"""Container entry point: Python by default, optional Rust byte delivery."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import shutil
import socket
import sys

import uvicorn

from sidecar.gateway.config import Settings


def relay_event(store, line: bytes) -> None:
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError):
        store.emit("media.relay_log_invalid", engine="rust")
        return
    if not isinstance(event, dict) or event.get("event") != "relay.closed":
        return
    fields = {name: event[name] for name in (
        "status", "bytes", "duration_ms", "reason", "first_body_ms", "upstream_headers_ms",
        "parent_trace", "method", "range", "control_ms",
    ) if name in event and isinstance(event[name], (str, int, float, bool))}
    count, duration = fields.get("bytes", 0), fields.get("duration_ms", 0)
    if isinstance(count, (int, float)) and isinstance(duration, (int, float)):
        fields["mib_per_s"] = round(count / 1048576 / max(duration / 1000, .001), 3)
    store.emit("media.closed", trace_id=event.get("trace_id", ""), engine="rust", **fields)


async def serve_rust(app, *, host: str, port: int, binary: str) -> int:
    from sidecar.gateway.media_engine import build_media_engine_router

    app.include_router(build_media_engine_router(app.state.playback))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    backend = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, access_log=False, timeout_graceful_shutdown=10,
        forwarded_allow_ips="127.0.0.1",
    ))
    process = None
    logging = None
    waiting = None

    async def stop_relay():
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 12)
            except TimeoutError:
                process.kill()
                await process.wait()
        if logging is not None:
            await logging

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application):
            try:
                yield
            finally:
                # Drain Rust's final events before TraceStore closes its rotating file.
                await stop_relay()

    app.router.lifespan_context = lifespan
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started or serving.done():
                break
            await asyncio.sleep(.05)
        if not server.started:
            app.state.traces.emit("runtime.relay_failed", reason="gateway_startup", engine="rust")
            return 1
        process = await asyncio.create_subprocess_exec(
            binary, "--gateway", backend, "--listen", f"{host}:{port}",
            "--read-timeout-ms", "25000", "--async-log",
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        if not isinstance(ready, dict) or ready.get("event") != "ready":
            raise ValueError("relay not ready")
        app.state.traces.emit("runtime.media_engine", engine="rust", reason="ready")

        async def collect():
            while line := await process.stdout.readline():
                # Disk rotation/console output never runs on the serving event loop.
                await asyncio.to_thread(relay_event, app.state.traces, line)

        logging = asyncio.create_task(collect())
        waiting = asyncio.create_task(process.wait())
        done, _ = await asyncio.wait({serving, waiting, logging}, return_when=asyncio.FIRST_COMPLETED)
        if serving not in done and not server.should_exit:
            app.state.traces.emit("runtime.relay_failed", reason="process_exit", engine="rust")
            return 1
        await serving
        return 0
    except (OSError, ValueError, TimeoutError):
        app.state.traces.emit("runtime.relay_failed", reason="process_start", engine="rust")
        return 1
    finally:
        await stop_relay()
        if waiting is not None:
            await waiting
        server.should_exit = True
        await serving
        sock.close()


def main() -> int:
    settings = Settings.from_env()
    host = os.getenv("T4_BIND_HOST", "0.0.0.0")
    port = int(os.getenv("T4_BIND_PORT", "8765"))
    binary = os.getenv("T4_RUST_RELAY_BIN") or shutil.which("t4-media-relay")
    if settings.media_engine == "rust" and not binary:
        print("Rust media module is missing; build the bundled binary or select python", file=sys.stderr)
        return 1
    from sidecar.gateway.main import app

    if settings.media_engine == "python":
        uvicorn.run(app, host=host, port=port, access_log=False)
        return 0
    return asyncio.run(serve_rust(app, host=host, port=port, binary=binary))


if __name__ == "__main__":
    raise SystemExit(main())
