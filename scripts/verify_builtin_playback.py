"""Read-only built-in HTTP gate. Never stores media URLs, tokens or headers."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time

import httpx
import uvicorn

from sidecar.at.subscription import resolve_subscription
from sidecar.compatibility.probes import media_kind
from sidecar.compatibility.runner import (
    CheckOptions, CompatibilityRunner, STAGES, artifact_identity, result_status,
)
from sidecar.diagnostics import TraceStore
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.playback import MediaAsset, PlaybackBridge
from sidecar.t4.adapter import T4Request
from sidecar.worker.client import WorkerRemoteError


@contextmanager
def local_service(subscription):
    with tempfile.TemporaryDirectory(prefix="t4-builtin-") as directory:
        app = create_app(Settings(
            subscription_url=subscription, plugin_dir=directory, worker_timeout=25,
            log_file=str(Path(directory) / "trace.jsonl"),
        ))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, access_log=False, log_level="critical",
        ))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                if not thread.is_alive():
                    raise RuntimeError("test_service_stopped")
                time.sleep(.05)
            if not server.started:
                raise RuntimeError("test_service_not_ready")
            yield f"http://127.0.0.1:{port}", app
        finally:
            server.should_exit = True
            thread.join(timeout=30)
            sock.close()
            if thread.is_alive():
                raise RuntimeError("test_service_shutdown_timeout")


def sample_media(payload, client):
    url, headers = payload.get("url"), payload.get("header") or {}
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return {"status": "unsupported", "reason": "non_http_media"}
    output, size = {}, None
    for label, range_value, include_headers in (
        ("first", "bytes=0-8191", True),
        ("middle", None, True),
        ("tail", "bytes=-8192", True),
        ("without_headers", "bytes=0-8191", False),
    ):
        if label == "middle":
            if size is None:
                output[label] = {"status": "not_tested", "reason": "unknown_length"}
                continue
            start = size // 2
            range_value = f"bytes={start}-{min(start+8191, size-1)}"
        try:
            started = time.monotonic()
            with client.stream("GET", url, headers={
                **(headers if include_headers else {}), "Range": range_value,
                "Accept-Encoding": "identity",
            }) as response:
                chunk = next(response.iter_bytes(8192), b"")
                bounds = response.headers.get("content-range", "")
                if "/" in bounds and bounds.rsplit("/", 1)[-1].isdigit():
                    size = int(bounds.rsplit("/", 1)[-1])
                output[label] = {
                    "http_status": response.status_code, "bytes": len(chunk),
                    "range_supported": response.status_code == 206 and bounds.startswith("bytes "),
                    "duration_ms": round((time.monotonic()-started)*1000),
                    "redirects": len(response.history),
                }
                if label == "first":
                    output[label]["container_signature"] = media_kind(chunk)
        except httpx.HTTPError as exc:
            output[label] = {"error": type(exc).__name__}
    output["status"] = "pass" if all(
        output[label].get("range_supported") for label in ("first", "middle", "tail")
    ) else "fail"
    return output


def developer_decode(payload, base, app):
    # This temporary byte relay is a test harness, not the production play result.
    key = app.state.playback._put(MediaAsset(
        None, payload["url"], payload.get("header") or {}, "application/octet-stream",
        "", time.monotonic(),
    ), ".bin")
    url = base + "/t4/media/" + key
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-rw_timeout", "20000000",
        "-analyzeduration", "5000000", "-probesize", "4000000",
        "-show_entries", "format=format_name,duration:stream=codec_name,codec_type,profile,width,height,pix_fmt",
        "-of", "json", url,
    ], capture_output=True, timeout=90)
    try:
        metadata = json.loads(probe.stdout)
    except ValueError:
        metadata = {}
    decoded = subprocess.run([
        "ffmpeg", "-v", "error", "-rw_timeout", "20000000", "-i", url,
        "-map", "0:v:0", "-map", "0:a:0", "-t", "3",
        "-progress", "pipe:1", "-nostats", "-f", "null", "-",
    ], capture_output=True, timeout=100)
    progress = {}
    for line in decoded.stdout.decode(errors="replace").splitlines():
        name, _, value = line.partition("=")
        if name in {"frame", "out_time_us"}:
            progress[name] = int(value.strip() or 0)
    return {
        "probe_returncode": probe.returncode, "metadata": metadata,
        "decode_returncode": decoded.returncode, "progress": progress,
        "stderr_bytes": len(decoded.stderr), "real_device_verified": False,
        "production_server_decodes_media": False,
    }


async def verify(base, args, app=None):
    source = SourceConfig("env-at", "authorized", subscription_url=os.environ["ALIST_TVBOX_SUBSCRIPTION_URL"])
    snapshot = await resolve_subscription(source)
    assert not snapshot.errors
    identity = artifact_identity()
    report = {
        "artifact": identity, "scope": "all_builtin_sources_sampled",
        "site_count": snapshot.site_count, "plugins": [], "screenshot_samples": [],
        "real_device_verified": False, "long_play_verified": False,
        "started_at": time.time(),
    }
    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=65) as http:
        class HttpAdapter:
            async def dispatch(self, request):
                response = await http.get(
                    f"/t4/fm/{request.source_id}/{request.plugin_id}/api/{request.module}",
                    params=request.params,
                )
                if response.status_code != 200:
                    body = response.json().get("detail", {})
                    code = body.get("code", "http_error") if isinstance(body, dict) else "http_error"
                    raise WorkerRemoteError(code, "redacted", {})
                return response.json()

        adapter = HttpAdapter()
        bridge, traces = PlaybackBridge(), TraceStore()
        runner = CompatibilityRunner(SourceRegistry((source,)), None, adapter, bridge, traces)
        network = httpx.Client(timeout=20, follow_redirects=True)
        try:
            for plugin in snapshot.plugins:
                if plugin.plugin_type != "builtin":
                    continue
                if not args.screenshot_only:
                    row = {"plugin_id": plugin.plugin_id, "api": plugin.api, "stages": {
                        stage: {"status": "not_tested", "reason": "pending"} for stage in STAGES
                    }}
                    metadata = plugin.public_metadata()
                    if metadata.get("t4_supported") is False:
                        row["stages"] = {
                            stage: {"status": "unsupported", "reason": metadata["compatibility_mode"]}
                            for stage in STAGES
                        }
                    else:
                        try:
                            await runner._check(source, plugin, row, CheckOptions(
                                keyword="" if plugin.api == "csp_Live" else args.keyword,
                                category_limit=2, line_limit=4,
                            ), base)
                        except Exception as exc:
                            row["stages"][row.get("active_stage", "home")] = {
                                "status": "fail", "error": type(exc).__name__,
                            }
                    row["status"] = result_status({
                        key: value for key, value in row["stages"].items()
                        if value.get("reason") != "declared_search_disabled"
                    })
                    report["plugins"].append(row)
                    print(json.dumps({"plugin": plugin.api, "status": row["status"]}), flush=True)
                if not args.keyword or plugin.api not in {"csp_XiaoYa", "csp_Media"}:
                    continue
                sample = {"api": plugin.api, "episodes": []}
                try:
                    async def call(module, params):
                        return await adapter.dispatch(T4Request(module, "env-at", plugin.plugin_id, params))
                    search = await call("search", {"wd": args.keyword, "quick": False})
                    card = next(v for v in search.get("list", []) if v.get("vod_name") == args.keyword)
                    detail = (await call("detail", {"ids": card["vod_id"]}))["list"][0]
                    flag = detail["vod_play_from"].split("$$$")[0]
                    entries = detail["vod_play_url"].split("$$$")[0].split("#")[:2]
                    for index, entry in enumerate(entries):
                        value = entry.partition("$")[2]
                        payload = await call("play", {"play": value, "flag": flag})
                        item = {
                            "episode": index+1, "parse": payload.get("parse"), "jx": payload.get("jx"),
                            "direct_http": "/t4/media/" not in payload.get("url", ""),
                            "header_count": len(payload.get("header") or {}),
                            "media": await asyncio.to_thread(sample_media, payload, network),
                        }
                        if args.decode and app is not None:
                            try:
                                item["developer_decode"] = await asyncio.to_thread(
                                    developer_decode, payload, base, app)
                            except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
                                item["developer_decode"] = {"error": type(exc).__name__}
                        sample["episodes"].append(item)
                except Exception as exc:
                    sample["error"] = type(exc).__name__
                report["screenshot_samples"].append(sample)
        finally:
            network.close()
            runner.probes.close()
            bridge.close()
            traces.close()
    report.update(
        summary=dict(Counter(row["status"] for row in report["plugins"])),
        artifact_current=artifact_identity() == identity, finished_at=time.time(),
    )
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url")
    parser.add_argument("--keyword", default="")
    parser.add_argument("--decode", action="store_true")
    parser.add_argument("--screenshot-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.decode and args.base_url:
        parser.error("--decode requires the task-owned local test service")
    if args.base_url:
        report = asyncio.run(verify(args.base_url.rstrip("/"), args))
    else:
        with local_service(os.environ["ALIST_TVBOX_SUBSCRIPTION_URL"]) as (base, app):
            report = asyncio.run(verify(base, args, app))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "screenshot_samples": report["screenshot_samples"],
                      "artifact_current": report["artifact_current"]}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"harness_error": type(exc).__name__}), flush=True)
        raise SystemExit(1)
