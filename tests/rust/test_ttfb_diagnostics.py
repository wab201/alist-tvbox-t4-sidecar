import asyncio
import hashlib
import struct
import sys
import time
from urllib.parse import urlsplit

import httpx
import pytest

from scripts.diagnose_rust_ttfb import detailed_pairs, diagnostic_load
from tests.rust.relay_support import binary_path, experiment, pattern
from tests.rust.wire_profile import WireProfile, parse_packet


def test_keeps_raw_timepoints_and_splits_source_body_preparation():
    samples = [{
        "trace": "a", "worker": 0, "request_number": 0,
        "client_start_us": 1000, "client_headers_us": 9000, "client_first_body_us": 10000,
        "ttfb_ms": 9, "chunks": 1, "hash_ms": .01,
        "transport_us": {
            "http11.send_request_headers.started": 2000,
            "http11.send_request_headers.complete": 2500,
        },
    }]
    origins = [{
        "trace": "a", "handler_us": 5000, "response_headers_us": 6000,
        "first_body_us": 7000, "first_body_sent_us": 7100,
    }]
    relays = [{
        "client_trace": "a", "handler_us": 3000, "upstream_request_us": 4000,
        "upstream_headers_us": 6500, "first_body_us": 8000,
    }]
    result = detailed_pairs(samples, origins, relays)
    row = result["samples"][0]
    assert row["detail_ms"]["client_pre_send"] == 1
    assert row["detail_ms"]["send_to_handler"] == .5
    assert row["detail_ms"]["origin_headers_to_body"] == 1
    assert row["detail_ms"]["origin_first_send"] == .1
    assert row["detail_ms"]["origin_first_to_relay_first"] == 1
    assert row["points_us"]["origin_first_body_sent"] == 7100
    assert sum(row["stages_ms"].values()) == row["ttfb_ms"]


def test_packet_parser_retains_only_synthetic_metadata():
    packet = bytearray(54)
    packet[12:14] = b"\x08\x00"
    packet[14] = 0x45
    packet[23] = 6
    packet[26:30] = packet[30:34] = b"\x7f\x00\x00\x01"
    struct.pack_into("!HH", packet, 34, 30001, 30002)
    packet[46] = 0x50
    packet += (
        b"GET /not-retained HTTP/1.1\r\nx-t4-relay-client-trace: diagnostic-a\r\n"
        b"Authorization: not-retained\r\n\r\n"
    )
    result = parse_packet(packet, 1234567, {30002})
    assert result["kind"] == "request" and result["trace"] == "diagnostic-a"
    assert result["timestamp_us"] == 1234567
    assert "not-retained" not in str(result)
    assert parse_packet(packet, 1234567, {30003}) is None
    packet[26:30] = b"\x0a\x00\x00\x01"
    assert parse_packet(packet, 1234567, {30002}) is None


@pytest.mark.skipif(sys.platform != "linux" or not binary_path().is_file(),
                    reason="requires the isolated Linux diagnostic environment")
@pytest.mark.requires_packet_capture
def test_kernel_filter_captures_request_timestamps_without_body_payloads():
    with experiment() as relays:
        url = relays["origin"].url
        with WireProfile({urlsplit(url).port}) as capture:
            response = httpx.get(url + "/file", trust_env=False, headers={
                "Range": "bytes=0-8191", "x-t4-relay-client-trace": "kernel-timing-test",
            })
            assert response.content == pattern(0, 8192)
    assert capture.dropped == 0
    assert any(row["trace"] == "kernel-timing-test" for row in capture.events)
    assert {"syn", "syn-ack", "request", "response"}.issubset(
        {row["kind"] for row in capture.events},
    )
    assert all(row["timestamp_us"] > 0 for row in capture.events)


@pytest.mark.skipif(not binary_path().is_file(), reason="experimental Rust binary not built")
@pytest.mark.parametrize("policy", ["original", "yield", "first-barrier"])
def test_diagnostic_interventions_keep_all_bytes_and_transport_trace(policy):
    with experiment(profile=True) as relays:
        samples, lag = asyncio.run(diagnostic_load(
            relays["origin"].url + "/file", concurrency=4, size=8192,
            expected=hashlib.sha256(pattern(0, 8192)).hexdigest(), policy=policy,
            trace_prefix=f"detail-{time.time_ns()}",
        ))
    assert len(samples) == 4
    for row in samples:
        assert row["bytes"] == 8192
        assert row["chunks"] > 0
        assert row["client_start_us"] <= row["client_headers_us"] <= row["client_first_body_us"]
        assert "http11.send_request_headers.complete" in row["transport_us"]
        assert row["hash_ms"] >= 0
    assert all(row["lag_ms"] >= 0 for row in lag)
