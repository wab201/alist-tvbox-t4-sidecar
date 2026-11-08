import asyncio
import json

import pytest

from scripts.diagnose_rust_python import compare, comparisons, node_pairs, wire_pairs
from tests.rust.relay_support import binary_path, experiment


def test_node_pairs_retain_additive_same_request_stages():
    samples = [{
        "trace": "a", "client_start_us": 1000, "client_headers_us": 8000,
        "client_first_body_us": 9000, "ttfb_ms": 8, "worker": 0,
        "chunks": 1, "hash_ms": 0, "transport_us": {},
    }]
    origin = [{
        "trace": "a", "handler_us": 4000, "response_headers_us": 5000,
        "first_body_us": 5100,
    }]
    relay = [{
        "client_trace": "a", "handler_us": 2000, "upstream_request_us": 3000,
        "upstream_headers_us": 6000, "response_ready_us": 6100,
        "body_poll_us": 6200, "first_body_us": 7000,
        "bytes": 42, "chunks": 2, "first_body_size": 20, "max_chunk_size": 22,
    }]
    row = node_pairs(samples, origin, relay)["samples"][0]
    assert sum(row["stages_ms"].values()) == 8
    assert row["detail_ms"]["headers_to_response"] == .1
    assert row["detail_ms"]["response_to_poll"] == .1
    assert row["detail_ms"]["poll_to_first"] == .8
    assert row["relay_body"]["chunks"] == 2


def test_wire_header_only_is_not_claimed_as_body_delivery():
    row = {
        "mode": "rust", "samples": [{"trace": "a", "client_port": 1200, "client_start_us": 1000}],
        "paired": {"samples": [{"trace": "a", "points_us": {
            "upstream_request": 2000, "origin_handler": 4000,
            "relay_first_body": 5000, "client_first_body": 8000,
        }}]},
        "wire_ports": {"origin": 1000, "rust": 1100},
        "wire_events": [
            {"kind": "request", "target_port": 1000, "trace": "a", "timestamp_us": 2100},
            {"kind": "response", "source_port": 1100, "target_port": 1200,
             "timestamp_us": 5100, "body_with_headers": False},
        ],
    }
    pair = wire_pairs(row)[0]
    assert not pair["downstream_body_with_headers"]
    assert pair["upstream_send_to_kernel_ms"] == .1
    assert pair["origin_kernel_to_handler_ms"] == 1.9
    assert pair["response_kernel_to_client_first_ms"] == 2.9


@pytest.mark.skipif(not binary_path().is_file(), reason="experimental Rust binary not built")
@pytest.mark.parametrize("yield_after_first", [False, True])
def test_both_real_transports_pair_complete_hashes_and_keep_connections(yield_after_first):
    with experiment(profile=True, yield_after_first_body=yield_after_first) as instances:
        rows = asyncio.run(compare(
            instances, size=8192, concurrency=2, batches=1, repeats=2,
            connections="per-worker",
        ))
    assert len(rows) == 8
    for row in rows:
        assert row["hashes_match"] and row["bytes"] == 16384
        assert row["paired"]["matched"] == row["paired"]["expected"] == 2
        assert row["consumer_connection_count"] == 2
        assert row["paired"]["negative_clock_samples"] == 0
        for paired in row["paired"]["samples"]:
            assert paired["relay_body"]["chunks"] >= 1
            assert paired["relay_body"]["max_chunk_size"] > 0
    assert len(comparisons(rows)) == 2
    assert "authorization" not in json.dumps(rows).lower()


@pytest.mark.skipif(not binary_path().is_file(), reason="experimental Rust binary not built")
def test_budget_caps_output_frames_without_copying_or_changing_media():
    with experiment(profile=True, delivery_budget_kib=256) as instances:
        rows = asyncio.run(compare(
            instances, size=2 * 1024 ** 2, concurrency=2, batches=1, repeats=1,
            connections="per-worker",
        ))
    for row in rows:
        assert row["hashes_match"]
        if row["mode"] == "rust":
            for pair in row["paired"]["samples"]:
                assert pair["relay_body"]["max_chunk_size"] <= 65536
                assert pair["relay_body"]["chunks"] >= pair["relay_body"]["upstream_chunks"]
