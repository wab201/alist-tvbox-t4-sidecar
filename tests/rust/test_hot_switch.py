import concurrent.futures
import json
import time

import psutil
import pytest

from tests.rust.test_optional_engine import gateway
from tests.rust.relay_support import pattern, HEADERS
from tests.unit.test_media_disconnect import wait_for


def test_switch_pins_inflight_stream_new_requests_and_process_identity(gateway):
    engine, base, origin, keys, client, directory, process = gateway
    before = [(child.pid, child.create_time()) for child in psutil.Process(process.pid).children()]
    path = base + "/t4/api/diagnostics/media-engine"
    state = client.get(path).json()
    assert state["hot_switch"] and state["rust_available"]
    other = "rust" if engine == "python" else "python"
    with client.stream("GET", base + "/t4/media/" + keys["slow"],
                       headers={"Range": "bytes=0-2097151"}) as old:
        chunks = old.iter_raw()
        content = next(chunks)
        assert old.headers["x-t4-media-engine"] == engine
        switched = client.put(path, json={"engine": other}).json()
        assert switched["engine"] == other and switched["generation"] == 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            def read(index):
                start = index * 8192
                response = client.get(base + "/t4/media/" + keys["file"], headers={
                    "Range": f"bytes={start}-{start+8191}",
                    "x-t4-relay-generation": "999", "x-t4-relay-selected": engine,
                    "x-t4-relay-trace": "f" * 32,
                    "x-forwarded-for": "203.0.113.9", "x-forwarded-proto": "https",
                })
                assert response.status_code == 206 and response.content == pattern(start, 8192)
                assert response.headers["x-t4-media-engine"] == other
                return response.headers["x-t4-trace-id"]
            traces = list(pool.map(read, range(16)))
        content += b"".join(chunks)
        assert len(content) == 2097152
        for offset in range(0, len(content), 65536):
            assert content[offset:offset+65536] == pattern(offset, 65536)
    assert [(child.pid, child.create_time()) for child in psutil.Process(process.pid).children()] == before
    assert json.loads((directory / "plugins/media-engine.json").read_text())["engine"] == other
    def closed():
        return [event for event in client.get(base + "/t4/api/diagnostics/events?limit=2000").json()["events"]
                if event["stage"] == "media.closed"]
    assert wait_for(lambda: len(closed()) == 17, seconds=3)
    rows = closed()
    old_event = next(event for event in rows if event["trace_id"] == old.headers["x-t4-trace-id"])
    assert old_event["engine"] == engine and old_event["engine_generation"] == 0
    for event in rows:
        assert event["range_kind"] == "bounded" and event["chunks"] > 0
        assert event["first_body_ms"] >= event["control_ms"] >= 0
        assert event["max_body_read_wait_ms"] <= event["body_read_wait_ms"]
        assert event["body_read_wait_ms"] <= event["duration_ms"] + 1
        if event["trace_id"] in traces:
            assert event["engine"] == event["selected_engine"] == other
            assert event["engine_generation"] == 1
    assert client.put(path, json={"engine": engine}).json()["generation"] == 2
    response = client.head(base + "/t4/media/" + keys["file"], headers={"Range": "bytes=3221225472-3221233663"})
    assert response.status_code == 206 and response.headers["x-t4-media-engine"] == engine
    bundle = client.get(base + "/t4/api/diagnostics/bundle").json()
    assert bundle["relay"]["logging"]["async"]
    assert any(event["stage"] == "media.ingress_closed" and event["first_body_ms"] >= 0
               for event in bundle["events"] if event.get("method") == "GET")
    for secret in (origin, HEADERS["Authorization"], HEADERS["Cookie"], "signature="):
        assert secret not in json.dumps(bundle)
    assert wait_for(lambda: client.get(origin + "/health").json()["active"] == 0)


def test_transform_contract_and_health_survive_repeated_switches(gateway):
    _, base, _, keys, client, *_ = gateway
    for engine in ("rust", "python", "rust", "python"):
        assert client.put(base + "/t4/api/diagnostics/media-engine", json={"engine": engine}).status_code == 200
        for key, status in (("inline", 200), ("hls", 200), ("direct", 302)):
            response = client.get(base + "/t4/media/" + keys[key])
            assert response.status_code == status
            assert response.headers["x-t4-media-engine"] == "python"
        assert client.get(base + "/t4/health").json()["media_engine"] == engine
    assert client.get(base + "/t4/config.json").json() == {"sites": []}


def test_running_transfer_emits_bounded_progress(gateway):
    _, base, _, keys, client, *_ = gateway
    with client.stream("GET", base + "/t4/media/" + keys["slow"],
                       headers={"Range": "bytes=0-37748735"}) as response:
        total = sum(len(chunk) for chunk in response.iter_raw())
    assert total == 37748736
    trace = response.headers["x-t4-trace-id"]
    def events():
        return client.get(base + "/t4/api/diagnostics/events", params={"trace": trace}).json()["events"]
    assert wait_for(lambda: any(row["stage"] == "media.closed" for row in events()), seconds=3)
    progress = [row for row in events() if row["stage"] == "media.progress"]
    assert 1 <= len(progress) <= 3
    assert all(0 < row["bytes"] <= total for row in progress)
