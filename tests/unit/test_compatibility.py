import asyncio
import json
import io
import os
import subprocess
import sys
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest
from PIL import Image

from sidecar.at.subscription import SubscriptionParser
from sidecar.compatibility.probes import ResourceProbes
from sidecar.compatibility.runner import CheckOptions, CompatibilityRunner, artifact_identity, episodes
from sidecar.diagnostics import TraceStore
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.playback import PlaybackBridge
from tests.unit.test_playback_diagnostics import dash_payload


SOURCE = SourceConfig("test", "Test", at_base_url="https://at.example", subscription_id="test")
SITES = {"sites": [{"key": "demo", "name": "Demo", "api": "csp_BiliBili"}]}
_png = io.BytesIO()
Image.new("RGB", (2, 2), "red").save(_png, format="PNG")
PNG = _png.getvalue()
MP4 = b"\x00\x00\x00\x20ftyp" + bytes(500000 - 8)
CARD = {"vod_id": "opaque-detail", "vod_name": "Private title",
        "vod_pic": "https://media.example/cover?secret=private-secret"}


def transport(request):
    if request.url.path == "/cover":
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})
    start, end = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
    end = min(end, len(MP4) - 1)
    return httpx.Response(206, content=MP4[start:end + 1], headers={
        "Content-Range": f"bytes {start}-{end}/{len(MP4)}", "Content-Type": "video/mp4",
    })


class Adapter:
    def __init__(self, hook=None):
        self.calls = []
        self.hook = hook

    async def dispatch(self, request):
        self.calls.append(request)
        if self.hook:
            custom = self.hook(request)
            if custom is not None:
                return custom
        if request.module == "home":
            return {"class": [{"type_id": "1", "type_name": "Films"}], "list": [dict(CARD)]}
        if request.module in {"search", "category"}:
            return {"list": [dict(CARD)]}
        if request.module == "detail":
            return {"list": [{**CARD, "vod_play_from": "Normal$$$操作",
                              "vod_play_url": "Episode$opaque-play$$$删除$dangerous-action"}]}
        if request.module == "play":
            assert request.params["play"] == "opaque-play"
            return {"url": "https://media.example/video.mp4?secret=private-secret", "header": {"Cookie": "private-cookie"}}
        raise AssertionError(request.module)


def runner_for(tmp_path, sites=SITES, adapter=None):
    snapshot = SubscriptionParser("https://at.example").parse("test", sites)

    async def resolve(_):
        return snapshot

    bridge = PlaybackBridge()
    runner = CompatibilityRunner(
        SourceRegistry((SOURCE,)), resolve, adapter or Adapter(), bridge, TraceStore(),
        report_path=str(tmp_path / "latest.json"),
    )
    runner.probes.client.close()
    runner.probes.client = httpx.Client(transport=httpx.MockTransport(transport))
    return runner, bridge


def run_once(runner, options=None):
    async def execute():
        runner.start(options or CheckOptions(), "http://local.example")
        await runner.task
        return runner.report
    return asyncio.run(execute())


def test_complete_chain_is_read_only_version_bound_and_redacted(tmp_path):
    runner, bridge = runner_for(tmp_path)
    try:
        result = run_once(runner)
        assert result["state"] == "completed"
        assert result["gate"]["passed"] is True
        assert result["progress"] == {"total": 1, "completed": 1}
        assert result["artifact"]["version"] == "0.2.1"
        assert result["artifact_current"] is True
        assert result["real_device_verified"] is result["long_play_verified"] is False
        assert result["remote_plugin_source_verified"] is False
        calls = runner.adapter.calls
        assert [request.module for request in calls] == ["home", "category", "search", "detail", "play"]
        assert calls[-2].params["ids"] == CARD["vod_id"]
        assert not any(request.module == "action" for request in calls)
        assert result["plugins"][0]["stages"]["media"]["lines"][0]["status"] == "pass"
        report = (tmp_path / "latest.json").read_text()
        for secret in ("private-secret", "private-cookie", "Private title", "media.example", "opaque-play", "opaque-detail"):
            assert secret not in report
    finally:
        runner.probes.close()
        bridge.close()


@pytest.mark.parametrize("module", ["home", "category", "search", "detail", "play"])
def test_stage_failure_does_not_abort_other_plugins_or_pass_gate(tmp_path, module):
    def hook(request):
        if request.module == module and request.plugin_id == "bad":
            raise RuntimeError("https://private.example/sub/private-secret")
    runner, bridge = runner_for(tmp_path, {"sites": [
        {"key": "bad", "api": "csp_BiliBili"}, {"key": "good", "api": "csp_BiliBili"},
    ]}, Adapter(hook))
    try:
        result = run_once(runner)
        assert result["gate"]["passed"] is False
        rows = {row["plugin_id"]: row for row in result["plugins"]}
        assert rows["bad"]["stages"][module]["status"] == "fail"
        assert rows["good"]["status"] == "pass"
        assert result["progress"]["completed"] == 2
        assert "private-secret" not in json.dumps(result)
    finally:
        runner.probes.close()
        bridge.close()


def test_all_subscription_entries_are_accounted_for_including_parse_failures(tmp_path):
    sites = {"spider": "https://at.example/spring.jar", "sites": [
        {"key": "demo", "api": "csp_BiliBili"},
        {"key": "push", "api": "csp_Push"},
        {"key": "page", "api": "csp_WebHome"},
        {"key": "unknown", "api": "unrecognized_runtime"},
        {"key": "broken", "ext": "invalid!"},
    ]}
    runner, bridge = runner_for(tmp_path, sites)
    try:
        result = run_once(runner)
        assert result["progress"] == {"total": 5, "completed": 5}
        assert result["gate"]["excluded_plugins"] == 3
        assert result["gate"]["passed"] is False
        assert result["sources"][0]["parse_errors"] == 1
        assert result["plugins"][-1]["classification"] == "invalid_descriptor"
        assert all(request.plugin_id == "demo" for request in runner.adapter.calls)
    finally:
        runner.probes.close()
        bridge.close()


@pytest.mark.parametrize("method,reason", [
    ("search", "empty_search"), ("detail", "empty_detail"),
])
def test_empty_results_never_pass(method, reason, tmp_path):
    adapter = Adapter(lambda request: {"list": []} if request.module == method else None)
    runner, bridge = runner_for(tmp_path, adapter=adapter)
    try:
        result = run_once(runner)
        stage = result["plugins"][0]["stages"][method]
        assert stage["status"] != "pass" and stage["reason"] == reason
        assert result["gate"]["passed"] is False
    finally:
        runner.probes.close()
        bridge.close()


def test_declared_search_disabled_is_explicit_not_an_error(tmp_path):
    runner, bridge = runner_for(tmp_path, {"sites": [
        {"key": "demo", "api": "csp_Media", "searchable": 0},
    ]})
    try:
        result = run_once(runner)
        assert result["plugins"][0]["stages"]["search"]["status"] == "unsupported"
        assert result["gate"]["passed"] is True
        assert not any(request.module == "search" for request in runner.adapter.calls)
    finally:
        runner.probes.close()
        bridge.close()

def test_search_can_use_a_real_card_from_another_plugin_without_hiding_empty_home(tmp_path):
    def hook(request):
        if request.plugin_id == "search-only" and request.module in {"home", "category"}:
            return {"list": []}
    runner, bridge = runner_for(tmp_path, {"sites": [
        {"key": "catalog", "api": "csp_BiliBili"},
        {"key": "search-only", "api": "csp_BiliBili"},
    ]}, Adapter(hook))
    try:
        report = run_once(runner, CheckOptions(concurrency=1))
        row = report["plugins"][1]
        assert row["stages"]["home"]["status"] == "blocked"
        assert row["stages"]["search"]["status"] == "pass"
        assert row["stages"]["search"]["sample_origin"] == "run_card"
        assert row["stages"]["detail"]["status"] == "pass"
        assert report["gate"]["passed"] is False
        assert "Private title" not in json.dumps(report)
    finally:
        runner.probes.close()
        bridge.close()


def test_numeric_zero_category_id_is_valid(tmp_path):
    def hook(request):
        if request.module == "home":
            return {"class": [{"type_id": 0, "type_name": "All"}], "list": [CARD]}
    runner, bridge = runner_for(tmp_path, adapter=Adapter(hook))
    try:
        result = run_once(runner)
        assert result["gate"]["passed"] is True
        assert runner.adapter.calls[1].params["t"] == "0"
    finally:
        runner.probes.close()
        bridge.close()


def test_unsupported_media_on_server_plugin_cannot_be_excluded_for_a_false_pass(tmp_path):
    def hook(request):
        return {"url": "push://unhandled"} if request.module == "play" else None
    runner, bridge = runner_for(tmp_path, adapter=Adapter(hook))
    try:
        result = run_once(runner)
        assert result["gate"]["passed"] is False
        assert result["gate"]["eligible_plugins"] == 1
        assert result["gate"]["excluded_plugins"] == 0
        assert result["plugins"][0]["stages"]["media"]["status"] == "unsupported"
    finally:
        runner.probes.close()
        bridge.close()


def test_bad_dash_preparation_is_reported_at_media_not_home(tmp_path):
    def hook(request):
        return {"url": "data:application/dash+xml;base64,broken!"} if request.module == "play" else None
    runner, bridge = runner_for(tmp_path, adapter=Adapter(hook))
    try:
        result = run_once(runner)
        stages = result["plugins"][0]["stages"]
        assert stages["home"]["status"] == "pass"
        assert stages["media"]["status"] == "fail"
    finally:
        runner.probes.close()
        bridge.close()


def test_duplicate_ids_cannot_certify_the_wrong_plugin(tmp_path):
    runner, bridge = runner_for(tmp_path, {"sites": [
        {"key": "demo", "api": "csp_Media"}, {"key": "demo", "api": "csp_BiliBili"},
    ]})
    try:
        result = run_once(runner)
        assert result["gate"]["passed"] is False
        assert len(result["plugins"]) == 2 and not runner.adapter.calls
    finally:
        runner.probes.close()
        bridge.close()


def test_line_limit_exposes_missing_coverage_and_prevents_pass(tmp_path):
    def hook(request):
        if request.module == "detail":
            return {"list": [{**CARD, "vod_play_from": "One$$$Two",
                              "vod_play_url": "One$opaque-play$$$Two$opaque-play"}]}
    runner, bridge = runner_for(tmp_path, adapter=Adapter(hook))
    try:
        result = run_once(runner, CheckOptions(line_limit=1))
        assert result["gate"]["passed"] is False
        row = result["plugins"][0]
        assert row["line_coverage"] == {"published": 2, "tested": 1, "not_tested": 1}
        assert row["stages"]["media"]["status"] == "not_tested"
    finally:
        runner.probes.close()
        bridge.close()


def test_safe_episode_filter_never_executes_action_entries():
    assert episodes({"vod_play_from": "操作", "vod_play_url": "删除$x"}) == []
    assert episodes({"vod_play_from": "Main", "vod_play_url": "检查更新$x#Episode$y"}) == [(1, "Main", "y")]


def test_folder_cards_use_category_navigation_before_detail(tmp_path):
    def hook(request):
        if request.module in {"home", "category"} and request.params.get("t") != "folder-id":
            return {"class": [{"type_id": "1", "type_name": "Films"}], "list": [
                {**CARD, "vod_id": "folder-id", "vod_tag": "folder"},
            ]}
    runner, bridge = runner_for(tmp_path, adapter=Adapter(hook))
    try:
        result = run_once(runner)
        assert result["gate"]["passed"] is True
        detail = result["plugins"][0]["stages"]["detail"]
        assert detail["navigation"] == [{"module": "category", "count": 1}]
        assert any(request.module == "category" and request.params["t"] == "folder-id"
                   for request in runner.adapter.calls)
    finally:
        runner.probes.close()
        bridge.close()


def test_cancellation_concurrency_and_single_task_guard(tmp_path):
    runner, bridge = runner_for(tmp_path)
    original = runner.adapter.dispatch
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(request):
        entered.set()
        await release.wait()
        return await original(request)

    runner.adapter.dispatch = slow

    async def execute():
        runner.start(CheckOptions(), "http://local")
        await entered.wait()
        with pytest.raises(RuntimeError):
            runner.start(CheckOptions(), "http://local")
        runner.cancel()
        release.set()
        await runner.task
        assert runner.report["state"] == "cancelled"
        assert runner.report["gate"]["passed"] is False
        assert len(runner.adapter.calls) == 1
    try:
        asyncio.run(execute())
    finally:
        runner.probes.close()
        bridge.close()


def test_interrupted_report_is_not_restored_as_a_pass(tmp_path):
    runner, bridge = runner_for(tmp_path)
    runner.report = {"state": "running", "gate": {"passed": True}}
    runner._save(json.dumps(runner.report))
    restored = CompatibilityRunner(runner.sources, runner.snapshot_for, runner.adapter, bridge,
                                   TraceStore(), report_path=str(tmp_path / "latest.json"))
    try:
        assert restored.report["state"] == "interrupted"
        assert restored.report["gate"]["passed"] is False
    finally:
        restored.probes.close()
        runner.probes.close()
        bridge.close()


def test_terminal_state_is_not_visible_before_runtime_cleanup(tmp_path):
    runner, bridge = runner_for(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def finish():
        entered.set()
        assert release.wait(5)
    runner.finish_runtime = finish

    async def execute():
        runner.start(CheckOptions(), "http://local")
        assert await asyncio.to_thread(entered.wait, 5)
        assert runner.report["state"] == "running"
        assert runner.report["gate"]["passed"] is False
        release.set()
        await runner.task
        assert runner.report["state"] == "completed"
        assert runner.report["gate"]["passed"] is True
    try:
        asyncio.run(execute())
    finally:
        release.set()
        runner.probes.close()
        bridge.close()


def test_process_startup_identity_cannot_be_replaced_by_new_disk_identity(tmp_path):
    runner, bridge = runner_for(tmp_path)
    runner.artifact["tree_sha256"] = "old-process"
    try:
        result = run_once(runner)
        assert result["gate"]["passed"] is False
        assert result["gate"]["reason"] == "artifact_changed"
    finally:
        runner.probes.close()
        bridge.close()


def test_report_write_failure_cannot_pass_gate(tmp_path):
    runner, bridge = runner_for(tmp_path)

    def fail(_):
        raise OSError("private-path-and-secret")
    runner._save = fail
    try:
        result = run_once(runner)
        assert result["state"] == "failed"
        assert result["gate"]["passed"] is False
        assert result["gate"]["reason"] == "report_write_failed"
        assert [error["phase"] for error in result["errors"]] == ["report_write", "final_report_write"]
        assert all(error["root_error"] == "OSError" for error in result["errors"])
        assert "private-path" not in json.dumps(result)
    finally:
        runner.probes.close()
        bridge.close()


def test_worker_persist_failure_keeps_cause_and_continues_other_workers(tmp_path):
    runner, bridge = runner_for(tmp_path, {"sites": [
        {"key": "first", "api": "csp_BiliBili"}, {"key": "second", "api": "csp_BiliBili"},
        {"key": "third", "api": "csp_BiliBili"},
    ]})
    save = runner._save
    calls = 0

    def fail_once(content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError(13, "private-path-and-secret")
        save(content)
    runner._save = fail_once
    try:
        result = run_once(runner)
        assert result["state"] == "failed"
        assert result["progress"] == {"total": 3, "completed": 3}
        assert result["gate"]["passed"] is False
        error = result["errors"][0]
        assert error["phase"] == "report_write"
        assert error["root_error"] == result["error"] == "PermissionError"
        assert error["errno"] == 13
        assert error["plugin_id"] in {"first", "second"}
        assert error["source_id"] == "test"
        assert error["error_file"] == "test_compatibility.py"
        assert error["error_function"] == "fail_once"
        assert "private-path" not in json.dumps(result)
        assert json.loads((tmp_path / "latest.json").read_text())["errors"] == result["errors"]
        assert any(event["stage"] == "compatibility.failed" for event in runner.traces.events())
    finally:
        runner.probes.close()
        bridge.close()


def test_final_runtime_cleanup_keeps_its_own_error_not_worker_failure(tmp_path):
    runner, bridge = runner_for(tmp_path)

    def finish():
        try:
            raise OSError(5, "private-runtime-path")
        except OSError as exc:
            raise RuntimeError("private-wrapper") from exc
    runner.finish_runtime = finish
    try:
        result = run_once(runner)
        assert result["state"] == "failed"
        assert result["gate"]["passed"] is False
        error = result["errors"][0]
        assert error["phase"] == "runtime_cleanup"
        assert error["error"] == "RuntimeError"
        assert error["root_error"] == "OSError"
        assert error["errno"] == 5
        assert "plugin_id" not in error
        assert "private-" not in json.dumps(result)
    finally:
        runner.probes.close()
        bridge.close()


def test_multiple_worker_errors_and_final_write_failure_are_all_retained(tmp_path):
    runner, bridge = runner_for(tmp_path, {"sites": [
        {"key": "first", "api": "csp_BiliBili"}, {"key": "second", "api": "csp_BiliBili"},
    ]})
    save = runner._save
    calls = 0

    def fail_after_initial(content):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise PermissionError(13, "private-persistent-path")
        save(content)
    runner._save = fail_after_initial
    try:
        result = run_once(runner)
        assert result["state"] == "failed"
        assert result["gate"]["passed"] is False
        assert result["gate"]["reason"] == "report_write_failed"
        assert [error["phase"] for error in result["errors"]] == [
            "report_write", "report_write", "final_report_write",
        ]
        assert len({error["plugin_id"] for error in result["errors"][:2]}) == 2
        assert "private-persistent" not in json.dumps(result)
        assert json.loads((tmp_path / "latest.json").read_text())["state"] == "running"
    finally:
        runner.probes.close()
        bridge.close()


def test_final_identity_read_failure_is_reported_not_an_unhandled_task(tmp_path, monkeypatch):
    runner, bridge = runner_for(tmp_path)

    def fail():
        raise PermissionError(13, "private-source-path")
    monkeypatch.setattr("sidecar.compatibility.runner.artifact_identity", fail)
    try:
        result = run_once(runner)
        assert result["state"] == "failed"
        assert result["gate"]["passed"] is result["artifact_current"] is False
        assert result["errors"][0]["phase"] == "artifact_identity"
        assert result["errors"][0]["root_error"] == "PermissionError"
        assert json.loads((tmp_path / "latest.json").read_text())["state"] == "failed"
        assert "private-source" not in json.dumps(result)
    finally:
        runner.probes.close()
        bridge.close()


def test_full_size_fixture_completes_and_persists_every_entry(tmp_path):
    runner, bridge = runner_for(tmp_path, {"sites": [
        {"key": f"fixture-{index}", "api": "csp_BiliBili"} for index in range(178)
    ]})
    try:
        result = run_once(runner)
        assert result["state"] == "completed" and result["errors"] == []
        assert result["progress"] == {"total": 178, "completed": 178}
        assert result["summary"] == {"pass": 178}
        assert result["gate"]["passed"] is True
        persisted = json.loads((tmp_path / "latest.json").read_text())
        assert persisted["progress"] == result["progress"]
        assert persisted["run_id"] == result["run_id"]
        assert persisted["state"] == "completed"
    finally:
        runner.probes.close()
        bridge.close()


def test_plugin_cleanup_error_is_redacted_and_cannot_pass(tmp_path):
    runner, bridge = runner_for(tmp_path, {"sites": [{
        "key": "raw", "api": "csp_PyProxy",
        "ext": {"source": "https://at.example/test.py", "loader": "https://at.example/Atvp.py", "raw": True},
    }]})

    async def release(*_):
        raise OSError(5, "private-cleanup-path")
    runner.release_plugin = release
    try:
        result = run_once(runner)
        assert result["gate"]["passed"] is False
        stage = result["plugins"][0]["stages"]["home"]
        assert stage["reason"] == "cleanup_error"
        assert stage["root_error"] == "OSError"
        assert stage["error_file"] == "test_compatibility.py"
        assert "private-cleanup" not in json.dumps(result)
    finally:
        runner.probes.close()
        bridge.close()


@pytest.mark.parametrize("winerror,attempts", [(32, 5), (5, 5), (3, 1), (None, 1)])
def test_report_replace_retries_only_bounded_sharing_violations(tmp_path, monkeypatch, winerror, attempts):
    from pathlib import Path
    runner, bridge = runner_for(tmp_path)
    calls, sleeps = [], []

    def replace(*_):
        calls.append(True)
        error = PermissionError(13, "private-report-path")
        if winerror is not None:
            error.winerror = winerror
        raise error
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr("sidecar.compatibility.runner.time.sleep", sleeps.append)
    try:
        with pytest.raises(PermissionError):
            runner._save("{}")
        assert len(calls) == attempts
        assert sleeps == ([.05, .1, .2, .4] if winerror in {5, 32} else [])
    finally:
        runner.probes.close()
        bridge.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing contract")
def test_real_windows_read_handle_does_not_abort_report_replacement(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes
    from pathlib import Path

    runner, bridge = runner_for(tmp_path)
    runner._save('{"old": true}')
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    # A normal reader shares reads/writes but not deletion of the open file.
    handle = kernel.CreateFileW(str(runner.report_path), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    attempted = threading.Event()
    violations = []
    replace = Path.replace

    def observed_replace(path, target):
        try:
            return replace(path, target)
        except OSError as exc:
            violations.append(getattr(exc, "winerror", None))
            attempted.set()
            raise
    monkeypatch.setattr(Path, "replace", observed_replace)

    def release():
        attempted.wait(5)
        time.sleep(.06)
        kernel.CloseHandle(handle)
    reader = threading.Thread(target=release)
    reader.start()
    try:
        runner._save('{"new": true}')
        assert violations and set(violations) <= {5, 32}
        assert json.loads(runner.report_path.read_text()) == {"new": True}
        assert not runner.report_path.with_suffix(".tmp").exists()
    finally:
        reader.join(6)
        runner.probes.close()
        bridge.close()


def test_cli_empty_registry_returns_gate_not_passed_exit_code(tmp_path):
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"ALIST_TVBOX_SUBSCRIPTION_URL", "T4_SOURCES_FILE", "T4_LOG_FILE",
                                  "T4_COMPATIBILITY_REPORT"}}
    environment["T4_PLUGIN_DIR"] = str(tmp_path / "plugins")
    result = subprocess.run([
        sys.executable, "-m", "scripts.check_compatibility", "--report", str(tmp_path / "report.json"),
    ], env=environment, capture_output=True, timeout=30)
    assert result.returncode == 2, result.stdout
    assert json.loads(result.stdout)["gate"]["passed"] is False


def test_compatibility_api_start_poll_export_without_extra_auth(tmp_path):
    async def fetch(_):
        return SITES
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry((SOURCE,)), fetch, Adapter())
    app.state.compatibility.probes.client.close()
    app.state.compatibility.probes.client = httpx.Client(transport=httpx.MockTransport(transport))
    with TestClient(app) as client:
        assert client.get("/t4/api/compatibility/latest").json() == {"report": None}
        assert client.get("/t4/api/compatibility/export").status_code == 404
        assert client.post("/t4/api/compatibility/runs", json={"concurrency": 99}).status_code == 422
        assert client.post("/t4/api/compatibility/runs", json={"source_ids": ["unknown"]}).status_code == 422
        assert client.post("/t4/api/compatibility/runs", json={}).status_code == 202
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.get("/t4/api/compatibility/latest").json()["report"]
            if result["state"] == "completed":
                break
            time.sleep(.02)
        assert result["gate"]["passed"] is True
        export = client.get("/t4/api/compatibility/export")
        assert export.headers["content-type"].startswith("application/json")
        assert export.json()["run_id"] == result["run_id"]
        app.state.compatibility.report["artifact"]["tree_sha256"] = "old"
        stale = client.get("/t4/api/compatibility/latest").json()["report"]
        assert stale["gate"] == {**stale["gate"], "passed": False, "reason": "artifact_changed"}
        assert client.post("/t4/api/compatibility/cancel").status_code == 202


@pytest.mark.parametrize("extension", ["py", "js"])
def test_uploaded_plugins_run_the_full_gate_in_real_workers(tmp_path, extension):
    home = json.dumps({"class": [{"type_id": "1", "type_name": "Films"}], "list": [CARD]})
    listing = json.dumps({"list": [CARD]})
    detail = json.dumps({"list": [{**CARD, "vod_play_from": "Normal", "vod_play_url": "Episode$raw-play"}]})
    play = json.dumps({"url": "https://media.example/video.mp4", "parse": 0})
    if extension == "py":
        source = f"""
from base.spider import Spider as Base
class Spider(Base):
    def init(self, extend): pass
    def homeContent(self, filter): return {home}
    def categoryContent(self, tid, pg, filter, extend): return {listing}
    def searchContent(self, key, quick, pg): return {listing}
    def detailContent(self, ids): return {detail}
    def playerContent(self, flag, id, vipFlags): return {play}
"""
    else:
        source = f"""module.exports = {{
init: () => {{}},
getName: () => "Gate fixture",
homeContent: () => ({home}),
categoryContent: () => ({listing}),
searchContent: () => ({listing}),
detailContent: () => ({detail}),
playerContent: () => ({play}),
}};"""
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    app.state.compatibility.probes.client.close()
    app.state.compatibility.probes.client = httpx.Client(transport=httpx.MockTransport(transport))
    with TestClient(app) as client:
        uploaded = client.post("/t4/api/plugins/upload", files={"file": (
            "gate." + extension, source.encode(),
        )})
        assert uploaded.status_code == 200
        api = uploaded.json()["plugin"]["fm_path"]
        assert client.get(api).status_code == 200
        assert client.post("/t4/api/compatibility/runs", json={}).status_code == 202
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            report = client.get("/t4/api/compatibility/latest").json()["report"]
            if report["state"] == "completed":
                break
            time.sleep(.05)
        assert report["gate"]["passed"] is True, report
        assert report["plugins"][0]["classification"] == ("raw_python" if extension == "py" else "javascript")
        assert client.get(api).status_code == 200


@pytest.fixture
def probes():
    bridge = PlaybackBridge()
    probes = ResourceProbes(bridge, httpx.MockTransport(transport))
    yield probes
    probes.close()
    bridge.close()


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>blocked</html>", headers={"Content-Type": "image/png"}),
    httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + bytes(100), headers={"Content-Type": "image/png"}),
    httpx.Response(403, content=PNG, headers={"Content-Type": "image/png"}),
])
def test_images_require_status_and_decodable_content(probes, response):
    probes.client.close()
    probes.client = httpx.Client(transport=httpx.MockTransport(lambda _: response))
    assert probes.image("https://media.example/cover")["status"] == "fail"


@pytest.mark.parametrize("mime", ["text/html", "image/jpeg", "application/octet-stream", ""])
def test_decodable_image_with_wrong_mime_is_not_reported_as_unavailable(probes, mime):
    probes.client.close()
    probes.client = httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(206, content=PNG, headers={"Content-Type": mime})
    ))
    result = probes.image("https://media.example/cover")
    assert result["status"] == "pass"
    assert result["warning"] == "mime_mismatch"
    assert result["evidence_level"] == "decoded_image"
    assert (result["width"], result["height"]) == (2, 2)


def test_remote_worker_root_error_is_retained_without_private_message(tmp_path):
    from sidecar.worker.client import WorkerRemoteError

    def hook(request):
        if request.module == "home":
            raise WorkerRemoteError("plugin_init_failed", "private-cookie", {
                "root_error": "ModuleNotFoundError", "missing_module": "shared_helper",
            })
    runner, bridge = runner_for(tmp_path, adapter=Adapter(hook))
    try:
        result = run_once(runner)
        stage = result["plugins"][0]["stages"]["home"]
        assert stage["root_error"] == "ModuleNotFoundError"
        assert stage["missing_module"] == "shared_helper"
        assert stage["status"] == "fail"
        assert "private-cookie" not in json.dumps(result)
    finally:
        runner.probes.close()
        bridge.close()


def test_image_headers_and_network_errors_are_not_exported(probes):
    def fail(request):
        assert request.headers["Referer"] == "https://private.example"
        raise httpx.ConnectError("private-secret")
    probes.client.close()
    probes.client = httpx.Client(transport=httpx.MockTransport(fail))
    result = probes.image('https://media.example/cover@{"Referer":"https://private.example"}')
    assert result["status"] == "blocked"
    assert "private" not in json.dumps(result)


def test_mp4_checks_start_middle_tail_and_rejects_html(probes):
    result = probes.media({"url": "https://media.example/video"})
    assert result["status"] == "pass"
    assert [item["position"] for item in result["samples"]] == ["start", "middle", "tail"]
    probes.client.close()
    probes.client = httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"<html>video.mp4</html>")
    ))
    assert probes.media({"url": "https://media.example/video"})["status"] == "fail"


def test_mp4_without_range_is_explicitly_blocked(probes):
    probes.client.close()
    probes.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=MP4[:100])))
    assert probes.media({"url": "https://media.example/video"})["reason"] == "range_unverified"


@pytest.mark.parametrize("encrypted,bad_segment", [(False, False), (True, False), (False, True)])
def test_hls_follows_actual_master_variant_and_two_segments(probes, encrypted, bad_segment):
    seen = []
    def hls(request):
        seen.append(request.url.path)
        assert request.headers["referer"] == "https://allowed.example"
        if request.url.path == "/master":
            body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nvariant.m3u8\n"
        elif request.url.path == "/variant.m3u8":
            key = '#EXT-X-KEY:METHOD=AES-128,URI="key"\n' if encrypted else ""
            body = ("#EXTM3U\n#EXT-X-TARGETDURATION:5\n" + key +
                    "#EXTINF:5,\none.ts\n#EXTINF:5,\ntwo.ts\n#EXT-X-ENDLIST\n").encode()
        else:
            body = b"<html/>" if bad_segment else (b"\x47" + bytes(187)) * 5
        return httpx.Response(200, content=body)
    probes.client.close()
    probes.client = httpx.Client(transport=httpx.MockTransport(hls))
    result = probes.media({"url": "https://video.example/master", "header": {"Referer": "https://allowed.example"}})
    assert result["status"] == ("fail" if encrypted or bad_segment else "pass")
    if encrypted:
        assert result["reason"] == "invalid_hls_key_length"
    if not encrypted and not bad_segment:
        assert seen == ["/master", "/variant.m3u8", "/one.ts", "/two.ts"]
        assert len(result["segments"]) == 2 and result["real_device_verified"] is False


def test_inline_dash_probes_both_tracks_via_bridge_output(probes):
    payload = probes.bridge.prepare(dash_payload(), "http://local.example")
    result = probes.media(payload)
    assert result["status"] == "pass"
    assert {item["kind"] for item in result["tracks"]} == {"audio", "video"}


def test_hls_external_audio_is_not_silently_ignored(probes):
    manifest = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,AUDIO="audio"
video.m3u8
"""
    probes.client.close()
    probes.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=manifest)))
    result = probes.media({"url": "https://media.example/master"})
    assert result["status"] == "fail"
    assert result["reason"] == "empty_media_playlist"


@pytest.mark.parametrize("payload,expected", [
    ({"url": "push://value"}, "unsupported"),
    ({"url": "https://media.example/video", "parse": 1}, "blocked"),
    ({}, "fail"),
])
def test_unsupported_or_client_parse_is_never_media_pass(probes, payload, expected):
    assert probes.media(payload)["status"] == expected
