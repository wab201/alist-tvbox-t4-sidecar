import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import pytest

from sidecar.diagnostics import TraceStore, media_summary, range_metadata, trace_scope
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.media_settings import MediaEngineSettings
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.gateway.streaming import ManagedStreamingResponse


def test_engine_persistence_and_noop_do_not_restart_or_change_generation(tmp_path):
    store = TraceStore(console=False)
    engine = MediaEngineSettings(tmp_path / "engine.json", "python", store)
    assert engine.snapshot()["engine"] == "python"
    with pytest.raises(RuntimeError):
        engine.update("rust")
    engine.available = True
    assert engine.update("rust")["generation"] == 1
    assert engine.update("rust")["generation"] == 1
    reloaded = MediaEngineSettings(engine.path, "python", store)
    assert reloaded.selected == "rust" and reloaded.generation == 1
    assert reloaded.snapshot()["engine"] == "python"  # Unloaded module is not reported as active.
    reloaded.available = True
    assert reloaded.snapshot()["engine"] == "rust"
    assert reloaded.update("python")["generation"] == 2
    assert [event["engine"] for event in store.events()] == ["rust", "python"]


def test_failed_persistence_keeps_previous_runtime_and_file(tmp_path, monkeypatch):
    engine = MediaEngineSettings(tmp_path / "engine.json", "python", TraceStore(console=False))
    engine.available = True
    engine.update("rust")
    original = engine.path.read_bytes()
    def failed(*_):
        raise OSError("disk error")
    monkeypatch.setattr(Path, "replace", failed)
    with pytest.raises(OSError):
        engine.update("python")
    assert engine.selected == "rust" and engine.generation == 1
    assert engine.path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [engine.path]


def test_slow_persistence_does_not_block_request_snapshot(tmp_path, monkeypatch):
    engine = MediaEngineSettings(tmp_path / "engine.json", "python", TraceStore(console=False))
    engine.available = True
    entered, release = threading.Event(), threading.Event()
    replace = Path.replace
    def slow_replace(path, target):
        entered.set()
        assert release.wait(5)
        return replace(path, target)
    monkeypatch.setattr(Path, "replace", slow_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = pool.submit(engine.update, "rust")
        try:
            assert entered.wait(2)
            state = pool.submit(engine.snapshot).result(timeout=1)
            assert state["engine"] == "python" and state["generation"] == 0
        finally:
            release.set()
        assert saved.result(timeout=2)["generation"] == 1
    assert engine.snapshot()["engine"] == "rust"


def test_engine_api_validation_availability_and_persistence_error(tmp_path, monkeypatch):
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with TestClient(app) as client:
        path = "/t4/api/diagnostics/media-engine"
        assert client.get(path).json()["engine"] == "python"
        assert client.put(path, json={"engine": "rust"}).status_code == 409
        assert client.put(path, json={"engine": "invalid"}).status_code == 422
        app.state.media_engine.available = True
        assert client.put(path, json={"engine": "rust"}).json()["generation"] == 1
        assert client.get("/t4/health").json()["media_engine"] == "rust"
        def failed(*_):
            raise PermissionError("private path")
        monkeypatch.setattr(Path, "replace", failed)
        response = client.put(path, json={"engine": "python"})
        assert response.status_code == 503 and "private path" not in response.text
        assert client.get(path).json()["engine"] == "rust"


def test_bounded_log_queue_never_blocks_media_and_reports_loss(tmp_path):
    store = TraceStore(str(tmp_path / "events.jsonl"), console=False)
    entered, release = threading.Event(), threading.Event()
    write = store._write_output
    def blocked(event):
        entered.set()
        assert release.wait(5)
        write(event)
    store._write_output = blocked
    store.start_output()
    try:
        store.emit("test", trace_id="safe")
        assert entered.wait(1)
        for _ in range(1200):
            store.emit("test", trace_id="safe", url="https://secret", Cookie="secret")
        status = store.output_status()
        assert status["pending"] == 1024 and status["dropped"] == 176
        assert len(store.events(limit=2000)) == 1201  # In-memory evidence is independent of disk loss.
    finally:
        release.set()
        store.close()
    assert not store._writer.is_alive()
    assert store.output_status()["pending"] == 0
    assert "secret" not in (tmp_path / "events.jsonl").read_text()


def test_log_device_failure_is_counted_without_erroring_request(tmp_path, monkeypatch):
    store = TraceStore(str(tmp_path / "events.jsonl"), console=False)
    def fail(_):
        raise OSError("secret path")
    monkeypatch.setattr(store._handler, "emit", fail)
    store.emit("media.closed", engine="python", bytes=0, reason="upstream_error")
    assert store.output_status()["write_errors"] == 1
    store.close()


def test_summary_is_versioned_success_only_and_does_not_pair_unrelated_media():
    rows = [{"schema": 2, "stage": "media.closed", "run_id": "run", "engine": "python",
             "selected_engine": "python", "engine_generation": 1, "reason": "complete",
             "status": 206, "first_body_ms": value, "bytes": 8} for value in range(1, 21)]
    rows.append({**rows[0], "reason": "upstream_error", "first_body_ms": 999})
    rows.append({**rows[0], "schema": 1})
    summary = media_summary(rows)
    assert len(summary) == 1
    assert summary[0]["completed"] == 20 and summary[0]["failed_or_cancelled"] == 1
    assert summary[0]["timings"]["first_body_ms"] == {"count": 20, "p50": 10, "p95": 19, "max": 20}
    assert "speedup" not in json.dumps(summary)


@pytest.mark.parametrize("header,fields", [
    ("bytes=3221225472-3221233663", {"range_kind": "bounded", "range_start": 3221225472, "range_end": 3221233663}),
    ("bytes=10-", {"range_kind": "open", "range_start": 10}),
    ("bytes=-16", {"range_kind": "suffix", "range_suffix": 16}),
    ("bytes=secret", {"range_kind": "other"}),
])
def test_range_logs_are_numeric_and_redacted(header, fields):
    assert range_metadata(header) == fields


def test_python_wait_measurements_separate_read_from_send():
    async def run():
        store = TraceStore(console=False)
        response = ManagedStreamingResponse(parent_trace="parent", stage="media")
        async def chunks():
            await asyncio.sleep(.025)
            response.count += 1024
            yield b"x" * 1024
        response.body_iterator = chunks()
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                await asyncio.sleep(.045)
        with trace_scope(store, trace_id="trace", engine_generation=4, selected_engine="python"):
            await response({"method": "GET", "type": "http", "headers": [], "asgi": {"spec_version": "2.4"}}, receive, send)
        event = next(row for row in store.events() if row["stage"] == "media.closed")
        assert event["body_read_wait_ms"] >= 20
        assert event["downstream_wait_ms"] >= 40
        assert event["chunks"] == 1 and event["first_body_size"] == 1024
        assert event["first_body_ms"] < event["duration_ms"]
        assert event["engine_generation"] == 4 and event["reason"] == "complete"
        assert store.media_status()["python"]["active"] == 0
    asyncio.run(run())


def test_performance_bundle_is_bounded_metadata_only(tmp_path):
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with TestClient(app) as client:
        app.state.traces.emit("media.closed", engine="python", reason="complete", status=200,
                              bytes=1024, first_body_ms=2.5, asset="public-key", url="private-url")
        response = client.get("/t4/api/diagnostics/bundle")
        bundle = response.json()
        assert response.headers["cache-control"] == "no-store"
        assert bundle["schema"] == 2 and bundle["logging"]["async"]
        assert "private-url" not in response.text
        assert bundle["summary"][0]["timings"]["first_body_ms"]["p95"] == 2.5
        assert bundle["measurement"]["bytes"] == "body_offered_not_client_acknowledged"
        assert client.get("/t4/api/diagnostics/media-summary").json()["groups"]
