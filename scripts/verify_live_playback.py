"""Read-only live AT playback checks. Output contains no deployment URLs or headers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any

import requests
import uvicorn

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.playback import MediaAsset


def run() -> dict[str, Any]:
    subscription = os.environ.get("ALIST_TVBOX_SUBSCRIPTION_URL")
    if not subscription:
        raise RuntimeError("ALIST_TVBOX_SUBSCRIPTION_URL is required")
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "sidecar").rglob("*.py")) + [
        root / "sidecar/web/index.html",
    ]
    report: dict[str, Any] = {
        "real_device_verified": False, "checks": {},
        "source_sha256": {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
        },
    }
    with tempfile.TemporaryDirectory(prefix="t4-playback-") as directory:
        app = create_app(Settings(
            subscription_url=subscription, plugin_dir=directory,
            worker_timeout=40, log_file=str(Path(directory) / "playback.jsonl"),
        ))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, access_log=False, log_level="critical",
        ))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(200):
                if server.started:
                    break
                if not thread.is_alive():
                    raise RuntimeError("test server stopped")
                time.sleep(.05)
            if not server.started:
                raise RuntimeError("test server did not start")
            session = requests.Session()

            def call(path: str, params=None, client=""):
                response = session.get(
                    base + path, params=params,
                    headers={"X-CLIENT": client} if client else {}, timeout=130,
                )
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP_{response.status_code}")
                return response.json(), response.headers.get("x-t4-trace-id", "")

            def first_detail(path: str):
                home, _ = call(path)
                items = list(home.get("list") or [])
                for category in (home.get("class") or [])[:4]:
                    if items:
                        break
                    listing, _ = call(path, {"t": category["type_id"], "pg": 1})
                    items = listing.get("list") or []
                if not items:
                    raise RuntimeError("no_catalog_items")
                result, _ = call(path, {"ids": items[0]["vod_id"]})
                return result["list"][0]

            def episode(vod):
                for flag, group in zip(
                    vod.get("vod_play_from", "").split("$$$"),
                    vod.get("vod_play_url", "").split("$$$"),
                ):
                    # Only the first ordinary episode is probed. Never execute action groups.
                    if flag in {"操作", "Actions"}:
                        continue
                    for item in group.split("#"):
                        label, _, value = item.partition("$")
                        if value:
                            return flag, value
                raise RuntimeError("no_episodes")

            snapshot, _ = call("/t4/api/sources/env-at/subscription")
            available = {p["api"]: p for p in snapshot["plugins"] if p.get("fm_path")}
            report["site_count"] = len(available)
            bili = available["csp_BiliBili"]["fm_path"]
            search, _ = call(bili, {"wd": "\u5341\u4e00\u5835\u8f66\u5fc5\u5907\u6b4c\u5355", "quick": "false"})
            matches = search.get("list") or []
            if matches:
                selected_card = next(
                    (item for item in matches if "900" in item.get("vod_name", "")),
                    matches[0],
                )
                detail, _ = call(bili, {"ids": selected_card["vod_id"]})
                vod = detail["list"][0]
                report["bili_sample"] = "screenshot_keyword"
            else:
                vod = first_detail(bili)
                report["bili_sample"] = "live_category"
            flag, value = episode(vod)
            for label, client in (("bili_generic", ""), ("bili_fongmi", "com.fongmi.android.tv")):
                try:
                    payload, trace = call(bili, {"play": value, "flag": flag}, client)
                    probe = app.state.playback.probe(payload)
                    check = {
                        "media": probe,
                        "http_url": str(payload.get("url", "")).startswith(("http://", "https://")),
                        "trace_id": trace,
                    }
                    # Give ffprobe a local range proxy so secrets never enter its command line.
                    if "/t4/media/" in str(payload.get("url", "")):
                        probe_url = payload["url"]
                    else:
                        bridge = app.state.playback
                        key = bridge._put(MediaAsset(
                            None, payload["url"], payload.get("header") or {},
                            "application/octet-stream", trace, time.monotonic(),
                        ), ".mp4")
                        probe_url = base + "/t4/media/" + key
                    result = subprocess.run([
                        "ffprobe", "-v", "error", "-rw_timeout", "25000000",
                        "-analyzeduration", "5000000", "-probesize", "2000000",
                        "-show_entries", "stream=codec_type,codec_name",
                        "-of", "json", probe_url,
                    ], capture_output=True, timeout=70)
                    try:
                        streams = json.loads(result.stdout).get("streams") or []
                    except ValueError:
                        streams = []
                    check["ffprobe"] = {
                        "returncode": result.returncode,
                        "streams": streams,
                        "audio_and_video": {"audio", "video"} <= {
                            stream.get("codec_type") for stream in streams
                        },
                    }
                    decoded = subprocess.run([
                        "ffmpeg", "-v", "error", "-rw_timeout", "25000000",
                        "-i", probe_url, "-map", "0:v:0", "-map", "0:a:0",
                        "-t", "5", "-progress", "pipe:1", "-nostats",
                        "-f", "null", "-",
                    ], capture_output=True, timeout=70)
                    progress = {}
                    for line in decoded.stdout.decode("utf-8", errors="replace").splitlines():
                        name, _, progress_value = line.partition("=")
                        if name in {"frame", "out_time_us"}:
                            progress[name] = int(progress_value.strip() or 0)
                    check["decode"] = {
                        "returncode": decoded.returncode,
                        "frames": progress.get("frame", 0),
                        "output_us": progress.get("out_time_us", 0),
                        "five_seconds_av": decoded.returncode == 0
                        and progress.get("frame", 0) > 0
                        and progress.get("out_time_us", 0) >= 5_000_000,
                    }
                    report["checks"][label] = check
                except Exception as exc:
                    report["checks"][label] = {"ok": False, "error": type(exc).__name__}
            if "csp_PianDan" in available:
                path = available["csp_PianDan"]["fm_path"]
                try:
                    home, _ = call(path)
                    card = home["list"][0]
                    detail, trace = call(path, {"ids": card["vod_id"]})
                    selected = episode(detail["list"][0])
                    play, _ = call(path, {"play": selected[1], "flag": selected[0]})
                    report["checks"]["piandan"] = {
                        "detail_groups": len(detail["list"][0]["vod_play_from"].split("$$$")),
                        "media": app.state.playback.probe(play), "trace_id": trace,
                    }
                except Exception as exc:
                    report["checks"]["piandan"] = {"ok": False, "error": type(exc).__name__}
            events, _ = call("/t4/api/diagnostics/events", {"limit": 2000})
            report["events"] = events["events"]
            report["log_events"] = len(events["events"])
            report["at_core_modified"] = False
            session.close()
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            sock.close()
            if thread.is_alive():
                raise RuntimeError("test server did not stop")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = run()
    except Exception as exc:
        # Exception text can contain credential-bearing URLs; never print it.
        print(json.dumps({"ok": False, "error": type(exc).__name__}))
        raise SystemExit(1)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "events"}, ensure_ascii=True))
