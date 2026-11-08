"""Task-owned real gateway fixture; no production subscriptions or plugin records."""
import argparse
import asyncio
import time

from fastapi import Request
from fastapi.responses import Response
import uvicorn

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.playback import MediaAsset
from sidecar.serve import serve_rust
from tests.rust.relay_support import HEADERS, binary_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--engine", required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    app = create_app(settings, SourceRegistry())
    bridge = app.state.playback
    targets = {
        "file": "/file?size=5368709120", "chunked": "/chunked?size=32768",
        "slow": "/slow", "truncated": "/truncated", "stall": "/stall/headers",
        "redirect": "/redirect", "cross": "/cross-redirect", "429": "/status/429",
        "signed": "/folder/space%20file.mkv?signature=a%2FB%2b&part=02&empty=&part=01",
    }
    keys = {}
    for name, path in targets.items():
        keys[name] = bridge._put(MediaAsset(
            None, args.origin + path, HEADERS.copy(), "application/octet-stream",
            "fixture-parent", time.monotonic(),
        ), ".bin")
    for name, asset in {
        "hls": MediaAsset(None, args.origin + "/manifest", {}, "application/vnd.apple.mpegurl",
                          "fixture-parent", time.monotonic(), "hls"),
        "inline": MediaAsset(b"fixture-inline", "", {}, "application/octet-stream",
                             "fixture-parent", time.monotonic()),
        "direct": MediaAsset(None, args.origin + "/file?size=32768", {}, "application/octet-stream",
                             "fixture-parent", time.monotonic(), redirectable=True),
    }.items():
        keys[name] = bridge._put(asset, ".m3u8" if name == "hls" else ".bin")
    # This fixture's loopback origin stands in for an authorized public CDN.
    bridge._direct_target = lambda url: url.startswith(args.origin)

    @app.get("/fixture/keys")
    async def fixture_keys():
        return keys

    @app.post("/fixture/echo")
    async def echo(request: Request):
        return Response(await request.body(), headers={
            "X-Echo-Host": request.headers["host"],
            "X-Echo-Private": request.headers.get("x-t4-relay-internal", "absent"),
        })

    if args.engine == "rust":
        raise SystemExit(asyncio.run(serve_rust(app, host="127.0.0.1", port=args.port,
                                              binary=str(binary_path()))))
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
