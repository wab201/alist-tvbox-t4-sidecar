from __future__ import annotations

import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
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
            try:
                self.handle_media(head=True)
            except OSError:
                pass

        def do_GET(self):
            self.handle_media(head=False)

        def handle_media(self, head):
            route = self.path.split("?", 1)[0].strip("/")
            mode = modes[route]
            events.append({"route": route, "method": self.command,
                           "range": self.headers.get("Range", ""),
                           "agent": self.headers.get("User-Agent", ""),
                           "if_range": self.headers.get("If-Range", ""),
                           "connection": self.client_address[1], "started": time.monotonic()})
            if head and mode == "slow-head":
                time.sleep(2.5)
            if mode == "denied" or head and mode == "nohead":
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            requests = [item for item in events if item["route"] == route and item["method"] == "GET"]
            if not head and mode == "unavailable-once" and len(requests) == 1:
                self.send_response(503)
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
            self.send_header("Connection", "keep-alive" if mode == "keepalive" else "close")
            self.send_header("Accept-Ranges", "bytes")
            if mode not in {"noetag", "noetag-truncate"}:
                changed = mode == "changed" or mode == "change-later" and len(requests) > 1
                self.send_header("ETag", '"different"' if changed else '"same-file"')
            if mode == "no-store":
                self.send_header("Cache-Control", "private, no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(CONTENT)}")
            self.end_headers()
            if not head:
                content = CONTENT[start:end + 1]
                if mode in {"truncate", "noetag-truncate", "stall"} or mode in {
                    "truncate-once", "stall-once",
                } and len(requests) == 1 or mode == "truncate-twice" and len(requests) <= 2 or (
                    mode == "stall-later" and len(requests) > 1
                ):
                    content = content[:8192]
                try:
                    self.wfile.write(content)
                    self.wfile.flush()
                    if mode == "stall" or mode == "stall-once" and len(requests) == 1 or (
                        mode == "stall-later" and len(requests) > 1
                    ):
                        time.sleep(6)
                except (ConnectionError, OSError):
                    pass
            self.close_connection = mode != "keepalive"

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
    assert relay[0]["range"] == f"bytes=16384-{len(CONTENT) - 1}"
    assert relay[0]["if_range"] == '"same-file"'
    assert relay[0]["agent"] != "signed-agent"


def test_successful_head_and_get_preserve_existing_connection_reuse(java, origin):
    origin[1]["direct"] = "keepalive"
    response = run(java, origin)
    assert not response["failed"]
    assert response["hash"] == hashlib.sha256(CONTENT).hexdigest()
    assert [event["method"] for event in origin[2]] == ["HEAD", "GET"]
    assert len({event["connection"] for event in origin[2]}) == 1


def test_single_byte_consumer_preserves_exact_range(java, origin):
    response = run(java, origin, range="bytes=12345-20536", action="single-byte")
    assert not response["failed"] and response["bytes"] == 8192
    assert response["hash"] == hashlib.sha256(CONTENT[12345:20537]).hexdigest()
    assert not any(event["route"] == "relay" for event in origin[2])


def test_slow_optional_head_does_not_delay_a_healthy_get(java, origin):
    origin[1]["direct"] = "slow-head"
    response = run(java, origin)
    assert not response["failed"]
    assert response["hash"] == hashlib.sha256(CONTENT).hexdigest()
    head, get = origin[2]
    assert head["method"] == "HEAD" and get["method"] == "GET"
    assert get["started"] - head["started"] < 2
    assert not any(event["route"] == "relay" for event in origin[2])


@pytest.mark.parametrize("mode", ["changed", "ignore-range", "denied", "truncate"])
def test_invalid_or_failing_repair_never_splices_wrong_bytes_or_retries_forever(java, origin, mode):
    origin[1].update(direct="truncate", relay=mode)
    response = run(java, origin)
    assert response["failed"]
    assert response["bytes"] <= 24576
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
    modes["direct"] = "truncate-twice"
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
        assert gets[-1]["range"] == f"bytes=16384-{len(CONTENT) - 1}"
        assert len(gets) == 3
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


@pytest.mark.parametrize("mode", ["truncate-once", "stall-once"])
def test_transient_failure_reconnects_direct_at_missing_byte_before_server(java, origin, mode):
    origin[1]["direct"] = mode
    response = run(java, origin)
    assert not response["failed"]
    assert response["hash"] == hashlib.sha256(CONTENT).hexdigest()
    gets = [event for event in origin[2] if event["method"] == "GET"]
    assert [event["route"] for event in gets] == ["direct", "direct"]
    assert gets[-1]["range"] == f"bytes=8192-{len(CONTENT) - 1}"
    assert all(event["if_range"] == '"same-file"' for event in gets)


def test_pause_resume_reopens_direct_without_server_or_duplicate_bytes(java, origin):
    origin[1]["direct"] = "truncate-once"
    response = run(java, origin, action="pause")
    assert not response["failed"]
    assert response["hash"] == hashlib.sha256(CONTENT).hexdigest()
    gets = [event for event in origin[2] if event["method"] == "GET"]
    assert [event["route"] for event in gets] == ["direct", "direct"]
    assert gets[-1]["range"] == f"bytes=8192-{len(CONTENT) - 1}"
    assert gets[-1]["started"] - gets[0]["started"] >= 15


def test_transient_upstream_503_does_not_force_media_through_server(java, origin):
    origin[1]["direct"] = "unavailable-once"
    response = run(java, origin)
    assert not response["failed"]
    assert response["hash"] == hashlib.sha256(CONTENT).hexdigest()
    assert [event["route"] for event in origin[2] if event["method"] == "GET"] == [
        "direct", "direct",
    ]


def test_repeated_stall_has_short_direct_retry_before_exact_server_repair(java, origin):
    origin[1]["direct"] = "stall"
    response = run(java, origin)
    assert not response["failed"]
    assert response["hash"] == hashlib.sha256(CONTENT).hexdigest()
    gets = [event for event in origin[2] if event["method"] == "GET"]
    assert [event["route"] for event in gets] == ["direct", "direct", "relay"]
    assert gets[-1]["range"] == f"bytes=16384-{len(CONTENT) - 1}"
    assert gets[-1]["started"] - gets[0]["started"] < 9


def test_cancel_while_reading_stalled_connection_does_not_restart_or_relay(java, origin):
    origin[1]["direct"] = "stall-later"
    response = run(java, origin, action="cancel-blocked")
    assert not response["failed"]
    assert [event["route"] for event in origin[2] if event["method"] == "GET"] == [
        "direct", "direct",
    ]


def test_cached_metadata_never_mixes_changed_object_and_is_invalidated(java, origin):
    origin[1].update(direct="change-later", relay="changed")
    response = run(java, origin, range="bytes=0-8191", action="cache-change")
    assert not response["failed"]
    assert response["hash"] == hashlib.sha256(CONTENT[:8192]).hexdigest()
    events = origin[2]
    assert len([event for event in events if event["method"] == "HEAD"]) == 2
    assert len([event for event in events if event["route"] == "relay"]) == 1


def test_metadata_cache_expires_after_short_ttl(java, origin):
    result = subprocess.run(
        java + [origin[0] + "/direct", origin[0] + "/relay", "bytes=0-8191", "", "cache-expiry"],
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stderr
    assert len([event for event in origin[2] if event["method"] == "HEAD"]) == 2


@pytest.mark.parametrize("mode,action,heads", [
    ("normal", "seek", 1), ("no-store", "seek", 4), ("normal", "seek-headers", 4),
])
def test_forward_backward_and_suffix_seeks_reuse_only_matching_metadata(
    java, origin, mode, action, heads,
):
    origin[1]["direct"] = mode
    result = subprocess.run(java + [origin[0] + "/direct", origin[0] + "/relay",
                                   "bytes=0-8191", "", action],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[3] == "false"
    expected = [CONTENT[100000:108192], CONTENT[:8192], CONTENT[-8192:]]
    assert result.stdout.splitlines()[5:] == [
        f"8192:{hashlib.sha256(part).hexdigest()}" for part in expected
    ]
    events = origin[2]
    assert len([event for event in events if event["method"] == "HEAD"]) == heads
    assert len([event for event in events if event["method"] == "GET"]) == 4
    assert not any(event["route"] == "relay" for event in events)
