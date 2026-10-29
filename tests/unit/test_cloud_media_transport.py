import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import re
import threading

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
import httpx

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from tests.unit.test_media_disconnect import serving, wait_for


def test_full_t4_cloud_flow_internal_origin_range_and_concurrent_sessions(tmp_path):
    upstream_app = FastAPI()
    calls = []
    lock = threading.Lock()
    data = bytes(range(251)) * 1024
    origin = ""

    @upstream_app.get("/play/fixture-token")
    async def play(request: Request):
        assert request.query_params["id"] == "1@99"
        assert request.query_params.get("type", "") == ""
        with lock:
            calls.append("play")
        return {
            "parse": 0, "url": origin + "/p/fixture-token/1@99.mkv?sign=exact%2Fvalue",
            "type": "PAN115", "header": {"User-Agent": "fixture-signed-ua"},
            "position": 120, "subs": [{"url": origin + "/sub.vtt"}],
        }

    @upstream_app.api_route("/internal/p/fixture-token/1@99.mkv", methods=["GET", "HEAD"])
    async def media(request: Request):
        assert request.url.query == "sign=exact%2Fvalue"
        assert request.headers["User-Agent"] == "fixture-signed-ua"
        assert request.headers["Accept-Encoding"] == "identity"
        with lock:
            calls.append((request.method, request.headers.get("range")))
        headers = {"ETag": '"fixture"', "Accept-Ranges": "bytes"}
        body, status = data, 200
        bounds = request.headers.get("range")
        if bounds and request.headers.get("if-range", '"fixture"') == '"fixture"':
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", bounds)
            assert match
            start = int(match[1]) if match[1] else max(0, len(data) - int(match[2]))
            end = min(len(data) - 1, int(match[2])) if match[1] and match[2] else len(data) - 1
            if start >= len(data) or end < start:
                return Response(status_code=416, headers={
                    **headers, "Content-Range": f"bytes */{len(data)}", "Content-Length": "0",
                })
            body, status = data[start:end + 1], 206
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        headers["Content-Length"] = str(len(body))
        if request.method == "HEAD":
            return Response(status_code=status, headers=headers)

        async def chunks():
            for offset in range(0, len(body), 4096):
                yield body[offset:offset + 4096]
                await asyncio.sleep(0)
        return StreamingResponse(chunks(), status_code=status, headers=headers)

    with serving(upstream_app) as upstream:
        origin = upstream
        source = SourceConfig(
            "demo", "Demo", subscription_url=origin + "/sub/fixture-token/0",
            cloud_playback="server", at_internal_url=origin + "/internal",
        )

        async def subscription(_):
            return {"sites": [{
                "key": "cloud", "name": "Cloud", "api": "csp_XiaoYa", "type": 3,
                "ext": {"api": origin, "token": "fixture-token",
                        "local_proxy_config": {"PAN115": {"enabled": True, "concurrency": 2}}},
            }]}

        app = create_app(
            Settings(plugin_dir=str(tmp_path)), SourceRegistry((source,)), subscription,
        )
        with serving(app) as gateway, httpx.Client(timeout=4) as client:
            targets = []
            for path, method, params in (
                ("/t4/fm/demo/cloud/", "GET", {"play": "1@99"}),
                ("/t4/fm/demo/cloud/api/play", "POST", {"play": "1@99"}),
                ("/t4/api/play", "POST", {"source": "demo", "site": "cloud", "play": "1@99"}),
            ):
                response = client.request(
                    method, gateway + path,
                    **({"params": params} if method == "GET" else {"json": params}),
                )
                assert response.status_code == 200
                payload = response.json()
                assert payload["header"] == {} and payload["parse"] == 0
                assert payload["type"] == "PAN115" and payload["position"] == 120
                assert payload["subs"] == [{"url": origin + "/sub.vtt"}]
                assert payload["url"].startswith(gateway + "/t4/media/")
                targets.append(payload["url"])

            def verify(target):
                response = client.head(target)
                assert response.status_code == 200 and not response.content
                assert response.headers["Content-Length"] == str(len(data))
                for bounds, expected in (
                    (None, data), ("bytes=0-8191", data[:8192]),
                    ("bytes=8192-", data[8192:]), ("bytes=-8192", data[-8192:]),
                ):
                    response = client.get(target, headers={
                        **({"Range": bounds} if bounds else {}), "If-Range": '"fixture"',
                    })
                    assert response.status_code == (206 if bounds else 200)
                    assert response.content == expected
                    assert response.headers["ETag"] == '"fixture"'
                    assert response.headers["X-Accel-Buffering"] == "no"
                response = client.get(target, headers={"Range": f"bytes={len(data)}-"})
                assert response.status_code == 416 and response.headers["Content-Range"] == f"bytes */{len(data)}"
                response = client.get(target, headers={"Range": "bytes=0-4", "If-Range": '"old"'})
                assert response.status_code == 200 and response.content == data

            with ThreadPoolExecutor(max_workers=3) as executor:
                list(executor.map(verify, targets))
            assert calls.count("play") == 3
            assert len(calls) == 24  # No prefetch, duplicate resolver, or Sidecar chunk fan-out.
            assert wait_for(lambda: len([
                event for event in app.state.traces.events() if event["stage"] == "media.closed"
            ]) == 21)
            events = client.get(gateway + "/t4/api/diagnostics/events", params={"limit": 2000}).json()["events"]
            relays = [item for item in events if item["stage"] == "playback.cloud_relay"]
            assert len(relays) == 3 and all(item["route"] == "internal" for item in relays)
            closed = [item for item in events if item["stage"] == "media.closed"]
            assert len(closed) == 21
            assert all(item["reason"] in {"complete", "client_disconnect"} for item in closed)
            assert all(isinstance(item["mib_per_s"], (float, int)) for item in closed)
            serialized = json.dumps(events)
            assert "fixture-token" not in serialized and "exact%2Fvalue" not in serialized
