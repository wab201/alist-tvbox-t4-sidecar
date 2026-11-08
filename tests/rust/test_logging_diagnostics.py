import concurrent.futures
import json
import time

import httpx
import pytest

from scripts.diagnose_rust_logging import log_delta
from tests.rust.relay_support import binary_path, experiment, pattern


def test_log_metrics_are_deltas_and_cumulative_max_is_explicit():
    before = {"events": 10, "event_build_ns": 100, "format_lock_write_flush_ns": 1000,
              "max_format_lock_write_flush_ns": 2000}
    after = {"events": 12, "event_build_ns": 200, "format_lock_write_flush_ns": 4001000,
             "max_format_lock_write_flush_ns": 3000000}
    row = log_delta(before, after)
    assert row["events"] == 2 and row["mean_write_ms"] == 2
    assert row["process_max_write_ms"] == 3
    with pytest.raises(ValueError, match="regressed"):
        log_delta(after, before)


@pytest.mark.skipif(not binary_path().is_file(), reason="release relay required")
def test_async_logging_drains_complete_error_and_cancel_events():
    with experiment(trace=True, relay_args=["--async-log", "--observe-log"],
                    log_read_delay=.02) as instances:
        base = instances["rust"].url
        def get(index):
            with httpx.Client(trust_env=False, timeout=5) as client:
                response = client.get(base + "/t4/media/file", headers={
                    "Range": "bytes=0-8191", "x-t4-relay-client-trace": f"logs-{index}",
                })
                assert response.status_code == 206 and response.content == pattern(0, 8192)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(get, range(24)))
        with httpx.Client(trust_env=False, timeout=5) as client:
            assert client.get(base + "/t4/media/unavailable").status_code == 502
            with client.stream("GET", base + "/t4/media/slow") as response:
                assert next(response.iter_raw(8192)) == pattern(0, 8192)
            deadline = time.monotonic() + 8
            while True:
                health = client.get(base + "/health").json()
                events = [json.loads(line) for line in instances["rust"].log.read_text().splitlines()]
                closed = [event for event in events if event.get("event") == "relay.closed"]
                if health["active"] == 0 and len(closed) == 26:
                    break
                assert time.monotonic() < deadline, "asynchronous log drain incomplete"
                time.sleep(.02)
        assert health["logging"]["async"]
        assert health["logging"]["capacity"] == 1024
        assert health["logging"]["pending"] == 0
        assert health["logging"]["written"] == health["requests"] == 26
        assert {event["reason"] for event in closed} == {"complete", "upstream_error", "client_disconnect"}
        assert all(health["logging"][key] == 0 for key in (
            "dropped_complete", "dropped_upstream_error", "dropped_cancelled", "write_errors",
        ))
