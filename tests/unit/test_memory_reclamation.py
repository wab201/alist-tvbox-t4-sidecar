import asyncio
from concurrent.futures import ThreadPoolExecutor
import time

from fastapi.testclient import TestClient

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.gateway.streaming import ManagedStreamingResponse
from sidecar.playback import MediaAsset, PlaybackBridge
from sidecar.resources import IdleMemory, ResourceActivityMiddleware
from sidecar.t4.opaque import OpaqueIdStore, OpaqueValue


def value(index):
    return OpaqueValue("source", "plugin", "play", f"private-{index}", "trace")


def test_opaque_hot_cache_is_bounded_but_cold_ids_and_trace_still_resolve():
    with_store = OpaqueIdStore(capacity=2, max_bytes=1024)
    try:
        tokens = [with_store.put(value(index)) for index in range(100)]
        assert len(with_store._values) <= 2 and with_store._size <= 1024
        assert with_store.release_memory() <= 2
        for index, token in enumerate(tokens):
            assert with_store.get(
                token, source_id="source", plugin_id="plugin", kind="play",
            ) == f"private-{index}"
            assert with_store.origin_trace(token) == "trace"
            assert with_store.get(
                token, source_id="other", plugin_id="plugin", kind="play",
            ) is None
        assert with_store._size <= 1024
    finally:
        with_store.close()
        with_store.close()


def test_oversized_id_never_pins_hot_memory_and_parallel_lookups_are_safe():
    store = OpaqueIdStore(capacity=2, max_bytes=256)
    try:
        entry = OpaqueValue("s", "p", "play", "x" * 8192)
        token = store.put(entry)
        assert not store._values and store._size == 0
        with ThreadPoolExecutor(max_workers=4) as threads:
            results = list(threads.map(
                lambda _: store.get(token, source_id="s", plugin_id="p", kind="play"),
                range(40),
            ))
        assert results == [entry.value] * 40
        assert not store._values
    finally:
        store.close()


def test_repeated_ids_are_deduplicated_without_changing_scope_or_origin_trace():
    store = OpaqueIdStore(capacity=1)
    try:
        first = store.put(value(1))
        assert store.put(value(1)) == first
        latest = OpaqueValue("source", "plugin", "play", "private-1", "latest")
        second = store.put(latest)
        assert second != first
        other = store.put(OpaqueValue("other", "plugin", "play", "private-1", "other"))
        assert other != first
        store.release_memory()
        assert store._db.execute("SELECT COUNT(*) FROM ids").fetchone()[0] == 3
        assert store.origin_trace(first) == "trace"
        assert store.origin_trace(second) == "latest"
        assert store.get(first, source_id="source", plugin_id="plugin", kind="play") == "private-1"
    finally:
        store.close()


def test_small_catalog_uses_hot_memory_without_synchronous_database_writes():
    store = OpaqueIdStore()
    try:
        for index in range(100):
            token = store.put(value(index))
            assert store.get(
                token, source_id="source", plugin_id="plugin", kind="play",
            ) == f"private-{index}"
        assert store._db.execute("SELECT COUNT(*) FROM ids").fetchone()[0] == 0
    finally:
        store.close()


def test_idle_memory_skips_active_recent_and_clean_intervals(monkeypatch):
    calls = []
    monkeypatch.setattr("sidecar.resources.release_unused_memory", lambda: (
        calls.append(True) or {"collected": 2, "trimmed": False}
    ))
    memory = IdleMemory()
    with memory.activity():
        assert memory.reclaim(idle_seconds=0)["reason"] == "active"
    assert memory.reclaim()["reason"] == "recent"
    assert memory.reclaim(idle_seconds=0)["reclaimed"]
    assert memory.reclaim(idle_seconds=0)["reason"] == "clean"
    assert len(calls) == 1


def test_newly_expired_media_is_reclaimed_without_another_http_request(monkeypatch):
    monkeypatch.setattr("sidecar.resources.release_unused_memory", lambda: {
        "collected": 0, "trimmed": False,
    })
    memory = IdleMemory()
    with memory.activity():
        pass
    assert memory.reclaim(idle_seconds=0, cleanup=lambda: 0)["reclaimed"]
    assert memory.reclaim(idle_seconds=0, cleanup=lambda: 0)["reason"] == "clean"
    result = memory.reclaim(idle_seconds=0, cleanup=lambda: 2)
    assert result["reclaimed"] and result["released_entries"] == 2


def test_resource_activity_lasts_until_stream_and_disconnect_cleanup_finish():
    memory = IdleMemory()

    async def app(scope, receive, send):
        assert memory.reclaim(idle_seconds=0)["reason"] == "active"
        await send({"type": "http.response.start", "status": 200})
        await asyncio.sleep(0)
        assert memory.reclaim(idle_seconds=0)["reason"] == "active"
        await send({"type": "http.response.body", "body": b"media", "more_body": False})
        assert memory.reclaim(idle_seconds=0)["reason"] == "active"

    async def send(message):
        pass

    asyncio.run(ResourceActivityMiddleware(app, memory)(
        {"type": "http", "path": "/t4/media/fixture"}, None, send,
    ))
    assert memory.reclaim(idle_seconds=0)["reclaimed"]


def test_disconnect_closes_body_generator_before_returning():
    async def run():
        started, closed = asyncio.Event(), asyncio.Event()

        async def chunks():
            try:
                yield b"media"
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.set()

        async def receive():
            await started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                # Disconnect while the producer is suspended at its yield.
                started.set()
                await asyncio.Event().wait()

        response = ManagedStreamingResponse(parent_trace="", stage="media")
        response.body_iterator = chunks()
        await response({
            "type": "http", "asgi": {"spec_version": "2.4"},
        }, receive, send)
        assert closed.is_set()
    asyncio.run(run())


def test_playback_pruning_preserves_recent_hls_parent_and_children():
    bridge = PlaybackBridge(ttl=30)
    try:
        old = bridge._put(MediaAsset(
            b"old", "", {}, "application/octet-stream", "", time.monotonic(),
        ), ".bin")
        live = bridge._put(MediaAsset(
            None, "https://media.example/main.m3u8", {}, "application/x-mpegurl",
            "", time.monotonic(), "hls",
        ), ".m3u8")
        child = bridge._hls_child(live, "https://media.example/1.ts", "segment", "")
        bridge._assets[old].created -= 60
        assert bridge.prune_expired() == 1
        assert bridge.get(old) is None
        assert bridge.get(child.rsplit("/", 1)[1]) is not None
        assert bridge._size == 2
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()
    assert not bridge._assets and bridge._size == 0


def test_gateway_lifespan_releases_cold_ids_and_stops_maintenance(tmp_path):
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with TestClient(app) as client:
        assert client.get("/t4/health").status_code == 200
        memory = app.state.memory
        with memory.activity():
            pass
        memory._last_used -= 60
        app.state.reclaim_resources()
        events = app.state.traces.events()
        assert any(row["stage"] == "gateway.memory_reclaimed" for row in events)
        assert memory.reclaim(idle_seconds=0)["reason"] == "clean"
