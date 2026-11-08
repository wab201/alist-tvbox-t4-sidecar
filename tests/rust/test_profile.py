import asyncio
import hashlib
import time

import httpx
import pytest

from scripts.benchmark_rust_media import independent_load, latency_summary, pair_profiles, transfer_load
from tests.rust.relay_support import PROFILE_HEADER, binary_path, experiment, pattern


def test_pairs_same_request_instead_of_subtracting_unrelated_percentiles():
    samples = [{
        "trace": "request-a", "client_start_us": 1000, "client_headers_us": 8000,
        "client_first_body_us": 9000, "ttfb_ms": 8,
    }]
    origin = [{
        "trace": "request-a", "handler_us": 4000, "response_headers_us": 5000,
        "first_body_us": 5100, "peer_port": 12345,
    }]
    relay = [{
        "client_trace": "request-a", "handler_us": 2000,
        "upstream_request_us": 3000, "upstream_headers_us": 6000, "first_body_us": 7000,
    }]
    result = pair_profiles(samples, origin, relay)
    assert result["matched"] == result["expected"] == 1
    assert result["negative_clock_samples"] == 0
    assert result["stage_p95_ms"]["upstream_dispatch"] == 1
    assert sum(result["samples"][0]["stages_ms"].values()) == 8
    assert pair_profiles(samples, [], relay)["matched"] == 0
    relay[0]["first_body_us"] = 9500
    assert pair_profiles(samples, origin, relay)["negative_clock_samples"] == 1


@pytest.mark.skipif(not binary_path().is_file(), reason="experimental Rust binary has not been built")
@pytest.mark.parametrize("fair", [False, True])
def test_fixture_timeline_matches_both_relays_without_logging_credentials(fair):
    with experiment(profile=True, fair_source=fair) as relays, httpx.Client(trust_env=False, timeout=5) as client:
        for mode in ("origin", "python", "rust"):
            path = "/file" if mode == "origin" else "/t4/media/file"
            result = independent_load(
                relays[mode].url + path, concurrency=2, duration=.05, size=8192,
                expected=hashlib.sha256(pattern(0, 8192)).hexdigest(),
                trace_prefix=f"profile-{mode}-{time.time_ns()}",
                min_requests=8,
            )
            deadline = time.monotonic() + 2
            while True:
                origins = client.get(relays["origin"].url + "/__profile").json()["events"]
                if mode == "rust":
                    import json
                    events = [json.loads(line) for line in relays["rust"].log.read_text(
                        encoding="utf-8",
                    ).splitlines()]
                else:
                    events = client.get(relays[mode].url + "/__profile").json()["events"]
                paired = pair_profiles(result["trace_samples"], origins, events,
                                       direct=mode == "origin")
                if paired["matched"] == paired["expected"] or time.monotonic() >= deadline:
                    break
                time.sleep(.01)
            assert paired["matched"] == paired["expected"] >= 2
            assert paired["negative_clock_samples"] == 0
            assert "authorization" not in str(paired).lower()
        response = client.get(relays["origin"].url + "/__profile")
        before = len(response.json()["events"])
        client.get(relays["python"].url + "/t4/media/file", headers={
            "Range": "bytes=0-15", PROFILE_HEADER: "invalid trace with spaces",
        })
        assert len(client.get(relays["origin"].url + "/__profile").json()["events"]) == before
        sampled = asyncio.run(transfer_load(
            relays["origin"].url + "/file", concurrency=2, duration=.001, size=8192,
            expected=hashlib.sha256(pattern(0, 8192)).hexdigest(),
            trace_prefix=f"sample-minimum-{time.time_ns()}", min_requests=7,
        ))
        assert sampled["requests"] >= 7


def test_sparse_tail_latency_is_labeled_and_empty_warm_samples_are_not_invented():
    assert latency_summary([]) == {"count": 0, "p95_ms": None, "p95_sample_warning": True}
    assert latency_summary([{"ttfb_ms": 8}])["p95_sample_warning"]
    assert not latency_summary([{"ttfb_ms": 8}] * 100)["p95_sample_warning"]


@pytest.mark.skipif(not binary_path().is_file(), reason="experimental Rust binary has not been built")
@pytest.mark.parametrize("mode", ["origin", "python", "rust"])
def test_discarded_large_source_body_stops_without_blocking_other_requests(mode):
    with experiment() as relays, httpx.Client(trust_env=False, timeout=2) as client:
        source = relays["origin"].url
        before = client.get(source + "/health").json()["generated_bytes"]
        path = "/file?size=5368709120" if mode == "origin" else "/t4/media/large"
        with client.stream("GET", relays[mode].url + path) as response:
            assert next(response.iter_raw(8192)) == pattern(0, 8192)
        deadline = time.monotonic() + 2
        while True:
            health = client.get(source + "/health").json()
            if health["active"] == 0 or time.monotonic() >= deadline:
                break
            time.sleep(.01)
        assert health["active"] == 0
        assert health["cancelled"] >= 1
        assert health["generated_bytes"] - before < 64 * 1024 ** 2
        assert client.head(relays[mode].url + path).status_code == 200
