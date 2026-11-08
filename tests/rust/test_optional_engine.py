import concurrent.futures
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest
from fastapi.testclient import TestClient

from sidecar.diagnostics import TraceStore
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.media_engine import raw_asset
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.playback import MediaAsset
from sidecar.serve import relay_event
from tests.rust.relay_support import HEADERS, ETAG, binary_path, origin_app, pattern, ROOT
from tests.unit.test_media_disconnect import serving, wait_for


@pytest.fixture(params=["python", "rust"])
def gateway(request, tmp_path):
    if not binary_path().is_file():
        pytest.skip("build Rust release binary first")
    with serving(origin_app()) as origin:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {**os.environ, "T4_MEDIA_ENGINE": request.param,
               "T4_LOG_FILE": str(tmp_path / "events.jsonl"),
               "T4_PLUGIN_DIR": str(tmp_path / "plugins"),
               "T4_SOURCES_FILE": "", "ALIST_TVBOX_SUBSCRIPTION_URL": ""}
        with (tmp_path / "runtime.log").open("wb") as log:
            process = subprocess.Popen([
                sys.executable, "-X", "utf8", "-m", "tests.rust.gateway_fixture",
                "--engine", request.param, "--origin", origin, "--port", str(port),
            ], cwd=ROOT, env=env, stdout=log, stderr=log)
            children = []
            try:
                base = f"http://127.0.0.1:{port}"
                with httpx.Client(trust_env=False, timeout=10) as client:
                    def ready():
                        if process.poll() is not None:
                            raise AssertionError("fixture gateway exited")
                        try:
                            return client.get(base + "/t4/health").status_code == 200
                        except httpx.HTTPError:
                            return False
                    assert wait_for(ready, seconds=25)
                    children = psutil.Process(process.pid).children(recursive=True)
                    keys = client.get(base + "/fixture/keys").json()
                    yield request.param, base, origin, keys, client, tmp_path, process
            finally:
                if process.poll() is None:
                    if os.name != "nt":
                        process.terminate()
                    else:
                        # Windows terminate() cannot deliver SIGTERM to Python.
                        for child in children:
                            if child.is_running():
                                child.terminate()
                        process.terminate()
                process.wait(20)
                for child in children:
                    try:
                        child.wait(3)
                    except psutil.TimeoutExpired:
                        child.kill()
                        child.wait(3)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_real_optional_engine_preserves_seek_headers_bytes_and_logs(gateway, method):
    engine, base, origin, keys, client, directory, _ = gateway
    offset = 3 * 1024 ** 3
    response = client.request(method, base + "/t4/media/" + keys["file"], headers={
        "Range": f"bytes={offset}-{offset+8191}", "If-Range": ETAG,
        "Authorization": "untrusted-player-header", "Origin": "https://player.example",
    })
    assert response.status_code == 206
    assert response.content == (pattern(offset, 8192) if method == "GET" else b"")
    assert response.headers["content-range"] == f"bytes {offset}-{offset+8191}/5368709120"
    assert response.headers["x-t4-media-engine"] == engine
    assert response.headers["access-control-allow-origin"] == "*"
    seen = client.get(origin + "/health").json()["seen"][-1]
    assert seen["authorization"] == HEADERS["Authorization"] and seen["method"] == method
    trace = response.headers["x-t4-trace-id"]
    def logged():
        return [e for e in client.get(base + "/t4/api/diagnostics/events").json()["events"]
                if e["stage"] == "media.closed" and e["trace_id"] == trace]
    assert wait_for(logged)
    event = logged()[0]
    assert event["engine"] == engine and event["reason"] == "complete"
    assert event["bytes"] == (8192 if method == "GET" else 0)
    assert event["parent_trace"] == "fixture-parent" and event["range"]
    if method == "GET":
        assert event["first_body_ms"] >= 0
    exported = client.get(base + "/t4/api/diagnostics/export").text
    for secret in (HEADERS["Authorization"], HEADERS["Cookie"], origin, "signature="):
        assert secret not in exported


def test_rust_gateway_retains_python_transform_and_direct_policy(gateway):
    engine, base, _, keys, client, *_ = gateway
    health = client.get(base + "/t4/health").json()
    assert health["media_engine"] == engine
    metrics = client.get(base + "/t4/api/diagnostics/media-engine").json()
    assert metrics["engine"] == engine
    if engine == "rust":
        assert metrics["logging"]["async"] and metrics["logging"]["capacity"] == 1024
    for key in ("hls", "inline", "direct"):
        response = client.get(base + "/t4/media/" + keys[key])
        assert response.headers["x-t4-media-engine"] == "python"
        if key == "hls":
            assert response.status_code == 200 and b"/t4/media/" in response.content
        elif key == "direct":
            assert response.status_code == 302 and response.content == b""
        else:
            assert response.content == b"fixture-inline"
    assert client.get(base + "/t4/media/not-found").status_code == 410


def test_optional_gateway_forwarding_and_loopback_control_channel(gateway):
    engine, base, _, _, client, *_ = gateway
    body = bytes(range(256)) * 1024
    response = client.post(base + "/fixture/echo?x=%2F", content=body, headers={
        "Host": "gateway.example", "x-t4-relay-internal": "client-cannot-use-this",
    })
    assert response.content == body and response.headers["x-echo-host"] == "gateway.example"
    if engine == "rust":
        assert response.headers["x-echo-private"] == "absent"
    for path in ("/__t4_relay/asset/x", "/%5f%5ft4_relay/asset/x"):
        response = client.get(base + path)
        assert response.status_code == 404
    assert client.get(base + "/t4/config.json").json() == {"sites": []}
    assert client.get(base + "/t4/assets/icons/save.svg").status_code == 200
    response = client.options(base + "/t4/media/x", headers={
        "Origin": "https://player.example", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Range",
    })
    assert response.status_code == 200


def test_optional_engine_errors_redirects_concurrency_and_disconnect(gateway):
    _, base, origin, keys, client, *_ = gateway
    for key in ("redirect", "cross", "signed"):
        response = client.get(base + "/t4/media/" + keys[key], headers={"Range": "bytes=0-8191"})
        assert response.status_code == 206 and response.content == pattern(0, 8192)
    response = client.get(base + "/t4/media/" + keys["429"])
    assert response.status_code == 429 and response.headers["retry-after"] == "17"
    with pytest.raises(httpx.RemoteProtocolError):
        client.get(base + "/t4/media/" + keys["truncated"])
    def read(n):
        response = client.get(base + "/t4/media/" + keys["file"],
                              headers={"Range": f"bytes={n*8192}-{n*8192+8191}"})
        assert response.status_code == 206 and response.content == pattern(n * 8192, 8192)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(read, range(16)))
    with client.stream("GET", base + "/t4/media/" + keys["slow"]) as response:
        assert next(response.iter_raw())
    assert wait_for(lambda: client.get(origin + "/health").json()["active"] == 0)


def test_engine_validation_and_logs_are_metadata_only(monkeypatch):
    monkeypatch.setenv("T4_MEDIA_ENGINE", "rust")
    assert Settings.from_env().media_engine == "rust"
    monkeypatch.setenv("T4_MEDIA_ENGINE", "typo")
    with pytest.raises(ValueError):
        Settings.from_env()
    store = TraceStore(console=False)
    relay_event(store, json.dumps({"event": "relay.closed", "trace_id": "test",
        "bytes": 1024, "duration_ms": 100, "reason": "complete", "url": "secret",
        "headers": {"Cookie": "private"}, "parent_trace": "parent"}).encode())
    event = store.events()[0]
    assert event["engine"] == "rust" and "url" not in event and "headers" not in event


def test_engine_process_ownership_and_fail_stop(gateway):
    engine, _, _, _, _, _, process = gateway
    children = psutil.Process(process.pid).children()
    assert len(children) == 1
    children[0].terminate()
    assert process.wait(15) == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows terminate is not POSIX SIGTERM")
def test_graceful_container_shutdown(gateway):
    _, _, _, _, _, _, process = gateway
    children = psutil.Process(process.pid).children()
    process.terminate()
    assert process.wait(20) in {0, -signal.SIGTERM}
    assert all(not child.is_running() for child in children)


def test_python_proxy_and_session_dependencies_stay_with_python(tmp_path, monkeypatch):
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    bridge = app.state.playback
    with TestClient(app):
        key = bridge._put(MediaAsset(None, "https://fixture.example/file", {}, "video/mp4",
            "trace", time.monotonic()), ".bin")
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        assert raw_asset(bridge, key)[0] is not None
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
        assert raw_asset(bridge, key)[1] == "python_proxy"
        monkeypatch.delenv("HTTPS_PROXY")
        bridge.media_client.cookies.set("session", "fixture-session", domain="fixture.example")
        assert raw_asset(bridge, key)[1] == "python_session"


@pytest.mark.parametrize("kind,redirect,content", [
    ("segment", False, None), ("hls", False, None), ("at_proxy", True, None),
    ("binary", False, b"inline"), ("key", False, None),
])
def test_handoff_never_bypasses_transform_or_redirect(tmp_path, kind, redirect, content):
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    bridge = app.state.playback
    with TestClient(app):
        key = bridge._put(MediaAsset(content, "https://fixture.example/file", {}, "video/mp4",
            "trace", time.monotonic(), kind, redirectable=redirect), ".bin")
        assert raw_asset(bridge, key)[0] is None
