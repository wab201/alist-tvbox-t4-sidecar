import hashlib
import json
import time

import httpx
import pytest

from scripts.diagnose_rust_startup import probe_effects
from tests.rust.relay_support import binary_path, experiment, pattern


def test_probe_cost_keeps_same_round_differences():
    rows = [{"phase": "overhead", "concurrency": 16, "repeat": repeat, "probe": probe,
             "hashes_match": True, "ttfb_p95_ms": 20 + (repeat if probe else 0),
             "mib_per_second": 100}
            for repeat in range(3) for probe in (False, True)]
    assert probe_effects(rows)[0]["p95_changes_ms"] == [0, 1, 2]
    with pytest.raises(AssertionError):
        probe_effects(rows[:-1])


@pytest.mark.skipif(not binary_path().is_file(), reason="experimental binary not built")
def test_startup_probe_is_opt_in_and_preserves_bytes():
    with experiment(trace=True, relay_args=("--observe-startup",)) as instances:
        with httpx.Client(trust_env=False, timeout=5) as client:
            for enabled in (False, True):
                trace = "probe-" + str(enabled)
                headers = {"Range": "bytes=0-8191", "x-t4-relay-client-trace": trace}
                if enabled:
                    headers["x-t4-relay-observe"] = "1"
                response = client.get(instances["rust"].url + "/t4/media/file", headers=headers)
                assert response.status_code == 206
                assert hashlib.sha256(response.content).digest() == hashlib.sha256(pattern(0, 8192)).digest()
                deadline = time.monotonic() + 2
                while True:
                    events = [json.loads(line) for line in instances["rust"].log.read_text().splitlines()]
                    found = [e for e in events if e.get("client_trace") == trace]
                    if found or time.monotonic() > deadline:
                        break
                    time.sleep(.01)
                assert len(found) == 1
                assert ("open_observation" in found[0]) == enabled
                if enabled:
                    for key in ("open_observation", "body_observation"):
                        observation = found[0][key]
                        assert observation["polls"] > 0
                        assert observation["ready_us"] >= observation["first_poll_us"] > 0
                        assert observation["pending_ns"] >= observation["wake_to_repoll_ns"]
