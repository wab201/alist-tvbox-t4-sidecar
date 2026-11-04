from __future__ import annotations

import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import threading
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
CONTENT = bytes(range(256)) * 1024


@pytest.fixture(scope="module")
def java(tmp_path_factory):
    home = os.getenv("JAVA_HOME")
    fallback = Path("C:/Program Files/Android/Android Studio/jbr/bin")
    suffix = ".exe" if os.name == "nt" else ""
    javac = str(Path(home) / "bin" / ("javac" + suffix)) if home else (
        shutil.which("javac") or str(fallback / "javac.exe")
    )
    java = str(Path(home) / "bin" / ("java" + suffix)) if home else (
        shutil.which("java") or str(fallback / "java.exe")
    )
    if not Path(javac).is_file() or not Path(java).is_file():
        pytest.skip("JDK is needed for the real-socket client delivery gate")
    classes = tmp_path_factory.mktemp("fm-java")
    result = subprocess.run([
        javac, "-encoding", "UTF-8", "--release", "8", "-Xlint:-options", "-d", str(classes),
        str(ROOT / "client/fm/src/net/t4/accelerator/RangeRelay.java"),
        str(ROOT / "tests/client/RangeRelayHarness.java"),
    ], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return [java, "-cp", str(classes), "RangeRelayHarness"]


@pytest.fixture(scope="module")
def probe_java(java, tmp_path_factory):
    classes = tmp_path_factory.mktemp("fm-probe-java")
    compiler = Path(java[0]).with_name("javac.exe" if os.name == "nt" else "javac")
    sources = [
        *sorted((ROOT / "client/fm/src").rglob("*.java")),
        ROOT / "client/fm/compile-only/com/github/catvod/crawler/Spider.java",
        *sorted((ROOT / "tests/client/fm-fixture").rglob("*.java")),
        ROOT / "tests/client/ProbeHarness.java",
    ]
    result = subprocess.run([
        str(compiler), "-encoding", "UTF-8", "--release", "8", "-Xlint:-options",
        "-d", str(classes), *map(str, sources),
    ], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return [java[0], "-cp", str(classes), "ProbeHarness"], classes


@pytest.mark.parametrize("mode,ready", [
    ("supported", True), ("no-fixed", False), ("no-range", False),
    ("no-head", False), ("wrong-body", False),
])
def test_compiled_init_only_confirms_after_loopback_http_capabilities_pass(
    probe_java, tmp_path, mode, ready,
):
    from sidecar.gateway.config import Settings
    from sidecar.gateway.main import create_app
    from sidecar.gateway.source_registry import SourceRegistry
    from tests.unit.test_media_disconnect import serving

    command, classes = probe_java
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with serving(app) as gateway:
        acceleration = app.state.acceleration
        session = acceleration.offer(gateway, "default")
        with zipfile.ZipFile(io.BytesIO(acceleration.artifact(session.id))) as archive:
            (classes / "t4-accelerator.properties").write_bytes(
                archive.read("t4-accelerator.properties"))
        result = subprocess.run(command + [mode], capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert (acceleration.get(session.id).state == "ready") is ready


@pytest.fixture
def origin():
    events = []
    modes = {"direct": "normal", "relay": "normal"}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.handle_media(head=True)

        def do_GET(self):
            self.handle_media(head=False)

        def handle_media(self, head):
            route = self.path.strip("/")
            mode = modes[route]
            events.append({"route": route, "method": self.command,
                           "range": self.headers.get("Range", ""),
                           "agent": self.headers.get("User-Agent", ""),
                           "if_range": self.headers.get("If-Range", "")})
            if mode == "denied" or head and mode == "nohead":
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end, status = 0, len(CONTENT) - 1, 200
            value = self.headers.get("Range", "")
            if value and not head and mode != "ignore-range":
                first, _, last = value.removeprefix("bytes=").partition("-")
                start = int(first) if first else max(0, len(CONTENT) - int(last))
                end = int(last) if first and last else end
                end = min(end, len(CONTENT) - 1)
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "bytes")
            if mode not in {"noetag", "noetag-truncate"}:
                self.send_header("ETag", '"different"' if mode == "changed" else '"same-file"')
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(CONTENT)}")
            self.end_headers()
            if not head:
                content = CONTENT[start:end + 1]
                requests = [item for item in events if item["route"] == route and item["method"] == "GET"]
                if mode in {"truncate", "noetag-truncate"} or mode == "truncate-once" and len(requests) == 1:
                    content = content[:8192]
                try:
                    self.wfile.write(content)
                    self.wfile.flush()
                except (ConnectionError, OSError):
                    pass
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", modes, events
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run(java, origin, *, range="", if_range="", action=""):
    base, _, _ = origin
    result = subprocess.run(java + [base + "/direct", base + "/relay", range, if_range, action],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    return {"status": int(lines[0]), "bytes": int(lines[1]), "hash": lines[2],
            "failed": lines[3] == "true", "range": lines[4] if len(lines) > 4 else ""}


@pytest.mark.parametrize("range,start,end,status", [
    ("", 0, len(CONTENT), 200),
    ("bytes=0-8191", 0, 8192, 206),
    ("bytes=45678-56789", 45678, 56790, 206),
    ("bytes=-8192", len(CONTENT) - 8192, len(CONTENT), 206),
    ("bytes=250000-", 250000, len(CONTENT), 206),
])
def test_continuous_direct_and_seek_do_not_use_server(java, origin, range, start, end, status):
    response = run(java, origin, range=range)
    assert response["status"] == status and not response["failed"]
    assert response["hash"] == hashlib.sha256(CONTENT[start:end]).hexdigest()
    assert not any(item["route"] == "relay" for item in origin[2])
    assert len(origin[2]) == 2  # One bounded HEAD and one continuous GET, not per-block requests.


def test_truncated_direct_is_repaired_from_exact_missing_byte(java, origin):
    origin[1]["direct"] = "truncate"
    response = run(java, origin)
    assert not response["failed"]
    assert response["hash"] == hashlib.sha256(CONTENT).hexdigest()
    relay = [event for event in origin[2] if event["route"] == "relay"]
    assert len(relay) == 1
    assert relay[0]["range"] == f"bytes=8192-{len(CONTENT) - 1}"
    assert relay[0]["if_range"] == '"same-file"'
    assert relay[0]["agent"] != "signed-agent"


@pytest.mark.parametrize("mode", ["changed", "ignore-range", "denied", "truncate"])
def test_invalid_or_failing_repair_never_splices_wrong_bytes_or_retries_forever(java, origin, mode):
    origin[1].update(direct="truncate", relay=mode)
    response = run(java, origin)
    assert response["failed"]
    assert response["bytes"] <= 16384
    assert len([event for event in origin[2] if event["route"] == "relay"]) == 1


def test_missing_validator_never_repairs_a_partially_delivered_file(java, origin):
    origin[1]["direct"] = "noetag"
    response = run(java, origin)
    assert not response["failed"]
    assert not any(event["route"] == "relay" for event in origin[2])


def test_missing_validator_truncation_remains_a_failure_not_a_clean_end(java, origin):
    origin[1]["direct"] = "noetag-truncate"
    response = run(java, origin)
    assert response["failed"] and response["bytes"] == 8192
    assert not any(event["route"] == "relay" for event in origin[2])


def test_initial_direct_rejection_can_select_relay_without_mixing(java, origin):
    origin[1]["direct"] = "denied"
    response = run(java, origin)
    assert not response["failed"]
    assert response["hash"] == hashlib.sha256(CONTENT).hexdigest()
    assert any(event["route"] == "relay" for event in origin[2])


def test_java_repair_through_real_sidecar_stream_preserves_object_headers_and_bytes(
    java, origin, tmp_path,
):
    from sidecar.gateway.config import Settings
    from sidecar.gateway.main import create_app
    from sidecar.gateway.source_registry import SourceRegistry
    from tests.unit.test_media_disconnect import serving

    base, modes, events = origin
    modes["direct"] = "truncate-once"
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with serving(app) as gateway:
        relay = app.state.playback.client_relay(base + "/direct", {
            "User-Agent": "signed-agent", "Referer": "https://origin.example/",
        }, gateway)
        result = subprocess.run(java + [base + "/direct", relay, "", ""],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert lines[2] == hashlib.sha256(CONTENT).hexdigest()
        assert lines[3] == "false"
        gets = [item for item in events if item["method"] == "GET"]
        assert len(gets) == 2
        assert gets[-1]["range"] == f"bytes=8192-{len(CONTENT) - 1}"
        assert all(item["agent"] == "signed-agent" for item in gets)


def test_head_has_no_body_download_and_cancel_does_not_launch_repair(java, origin):
    response = run(java, origin, action="head")
    assert response["bytes"] == 0
    assert [event["method"] for event in origin[2]] == ["HEAD"]
    origin[2].clear()
    response = run(java, origin, action="cancel")
    assert response["bytes"] <= 32768
    assert not any(event["route"] == "relay" for event in origin[2])


def test_if_range_mismatch_returns_whole_current_object(java, origin):
    response = run(java, origin, range="bytes=20000-30000", if_range='"old-object"')
    assert response["status"] == 200
    assert response["hash"] == hashlib.sha256(CONTENT).hexdigest()


@pytest.mark.parametrize("range", ["bytes=999999-", "bytes=3-1", "bytes=-0", "bytes=0-1,4-5"])
def test_unsatisfiable_ranges_are_explicit_without_downloading(java, origin, range):
    response = run(java, origin, range=range)
    assert response["status"] == 416 and response["bytes"] == 0
    assert response["range"] == f"bytes */{len(CONTENT)}"
    assert len(origin[2]) == 1
