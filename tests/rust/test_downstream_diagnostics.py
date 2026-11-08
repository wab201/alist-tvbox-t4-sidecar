import asyncio
import hashlib
import struct
import sys
from urllib.parse import urlsplit

import pytest

from scripts.benchmark_rust_media import transfer_load
from scripts.diagnose_rust_convoy import attribute, measured_downstream_load
from tests.rust.relay_support import binary_path, experiment, pattern
from tests.rust.test_convoy_diagnostics import sample
from tests.rust.wire_profile import FirstBodyWireProfile, ProcessFirstBodyWireProfile, packet_filter


def packet(payload, *, source=30002, target=30001, sequence=100, declared_length=None):
    value = bytearray(54)
    value[12:14] = b"\x08\x00"
    value[14] = 0x45
    value[23] = 6
    value[26:30] = value[30:34] = b"\x7f\x00\x00\x01"
    struct.pack_into("!H", value, 16, 40 + (len(payload) if declared_length is None else declared_length))
    struct.pack_into("!HHI", value, 34, source, target, sequence)
    value[46] = 0x50
    value[47] = 0x18
    return value + payload


def capture():
    result = FirstBodyWireProfile({30002}, body_port=30002, expected=1)
    result.record_packet(packet(
        b"GET /not-retained HTTP/1.1\r\nx-t4-relay-client-trace: body-test\r\n\r\n",
        source=30001, target=30002,
    ), 1000)
    return result


@pytest.mark.parametrize("combined", [False, True])
def test_first_body_is_not_confused_with_header_packet(combined):
    result = capture()
    headers = b"HTTP/1.1 206 Partial Content\r\nContent-Length: 2\r\n\r\n"
    result.record_packet(packet(headers + (b"xy" if combined else b"")), 2000)
    if not combined:
        assert not any(e["kind"] == "first-body" for e in result.events)
        result.record_packet(packet(b"xy", sequence=100 + len(headers)), 3000)
    body, = [e for e in result.events if e["kind"] == "first-body"]
    assert body["timestamp_us"] == (2000 if combined else 3000)
    assert body["trace"] == "body-test"
    assert "not-retained" not in str(result.events)
    assert "Content-Length" not in str(result.events)
    result.record_packet(packet(b"later-media", sequence=100 + len(headers) + 2), 4000)
    assert result.body_events == 1


def test_coalesced_body_detected_from_ip_length_without_retaining_payload():
    result = capture()
    headers = b"HTTP/1.1 206 Partial Content\r\nContent-Length: 8192\r\n\r\n"
    result.record_packet(packet(headers, declared_length=len(headers) + 8192), 2000)
    assert result.body_events == 1


def test_missing_first_body_sequence_is_rejected():
    result = capture()
    headers = b"HTTP/1.1 206 Partial Content\r\nContent-Length: 10\r\n\r\n"
    result.record_packet(packet(headers), 2000)
    with pytest.raises(RuntimeError, match="sequence missing"):
        result.record_packet(packet(b"missed-start", sequence=101 + len(headers)), 3000)


def run_filter(instructions, value):
    accumulator = index = x = 0
    while True:
        code, jt, jf, operand = instructions[index]
        if code in (0x28, 0x30, 0x40):
            offset = operand + (x if code == 0x40 else 0)
            length = {0x28: 2, 0x30: 1, 0x40: 4}[code]
            if offset + length > len(value):
                return 0
            accumulator = int.from_bytes(value[offset:offset + length], "big")
        elif code == 0x15:
            index += jt if accumulator == operand else jf
        elif code == 0x45:
            index += jt if accumulator & operand else jf
        elif code == 0x05:
            index += operand
        elif code == 0x54:
            accumulator &= operand
        elif code == 0x74:
            accumulator >>= operand
        elif code == 0x04:
            accumulator += operand
        elif code == 0x07:
            x = accumulator
        elif code == 0x06:
            return operand
        else:
            raise AssertionError(f"unsupported diagnostic BPF opcode {code}")
        index += 1


def test_filter_stops_completed_body_flows_but_preserves_other_streams_and_origin():
    body = packet(b"media", target=30001)
    other = packet(b"media", target=30003)
    request = packet(b"GET /fixture HTTP/1.1\r\n\r\n", source=30004, target=30005)
    assert run_filter(packet_filter(), body) == 0
    assert run_filter(packet_filter({30002}), body) == 512
    filtered = packet_filter({30002}, {30001})
    assert run_filter(filtered, body) == 0
    assert run_filter(filtered, other) == 512
    assert run_filter(filtered, request) == 512
    assert run_filter(filtered, packet(b"HTTP/1.1 206 Fixture\r\n\r\n", source=30005)) == 512
    assert run_filter(filtered, packet(b"irrelevant", source=30005)) == 0
    assert run_filter(filtered, b"") == 0


def test_process_collector_reports_startup_error_and_reclaims_child():
    capture = ProcessFirstBodyWireProfile({30002}, body_port=30003, expected=1)
    with pytest.raises(RuntimeError, match="collector failed"):
        with capture:
            pytest.fail("invalid collector must not start")
    assert capture.process._closed


def test_full_history_probe_requires_original_profiled_consumer(monkeypatch):
    from scripts.benchmark_rust_media import main

    monkeypatch.setattr(sys, "argv", ["benchmark", "--downstream-timing"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_downstream_spans_close_and_hash_overlap_is_not_added_twice():
    row = sample()
    row["startup_hash_intervals"] = [
        {"start_us": 1009400, "end_us": 1009700, "trace": "another-stream"},
    ]
    result = attribute(row, origin_port=9010, downstream=True, wire_events=[
        {"kind": "request", "target_port": 9010, "trace": "case-1", "timestamp_us": 1002500},
        {"kind": "first-body", "trace": "case-1", "timestamp_us": 1009500},
    ])
    item = result["p95_request"]
    assert item["spans_ms"]["relay_to_downstream_kernel"] == .5
    assert item["spans_ms"]["downstream_kernel_to_consumer"] == .5
    assert item["consumer_hash_overlap_ms"] == .2
    assert sum(item["spans_ms"].values()) == 10


def test_relay_request_capture_splits_client_wait_without_reusing_header_packets():
    row = sample()
    result = attribute(row, origin_port=9010, relay_port=9020, wire_events=[
        {"kind": "request", "target_port": 9010, "trace": "case-1", "timestamp_us": 1002500},
        {"kind": "request", "target_port": 9020, "trace": "case-1", "timestamp_us": 1000800},
        {"kind": "response", "target_port": 9020, "trace": "case-1", "timestamp_us": 1000500},
        {"kind": "request", "target_port": 9020, "trace": "other", "timestamp_us": 1000500},
    ])
    item = result["p95_request"]
    assert item["spans_ms"]["client_to_relay_kernel"] == .8
    assert item["spans_ms"]["relay_kernel_to_handler"] == .2
    assert "client_to_handler" not in item["spans_ms"]
    assert item["points_us"]["relay_kernel_request"] == 1000800
    assert sum(item["spans_ms"].values()) == 10


@pytest.mark.parametrize("times", [[], [1000800, 1000801], [1001100]])
def test_relay_request_capture_rejects_missing_duplicate_or_reversed_events(times):
    events = [
        {"kind": "request", "target_port": 9010, "trace": "case-1", "timestamp_us": 1002500},
        *({"kind": "request", "target_port": 9020, "trace": "case-1", "timestamp_us": value}
          for value in times),
    ]
    with pytest.raises(AssertionError, match="relay kernel request|clock"):
        attribute(sample(), origin_port=9010, relay_port=9020, wire_events=events)


def test_relay_request_split_requires_captured_events():
    with pytest.raises(AssertionError, match="requires capture"):
        attribute(sample(), relay_port=9020)


def test_fresh_client_handshake_separates_preconnect_and_connected_wait():
    row = sample()
    row["startup_hash_intervals"] = []
    result = attribute(row, origin_port=9010, relay_port=9020, downstream=True, wire_events=[
        {"kind": "request", "target_port": 9010, "trace": "case-1", "timestamp_us": 1002500},
        {"kind": "request", "source_port": 4000, "target_port": 9020,
         "trace": "case-1", "timestamp_us": 1000800},
        {"kind": "syn", "source_port": 4000, "target_port": 9020, "timestamp_us": 1000200},
        {"kind": "syn-ack", "source_port": 9020, "target_port": 4000, "timestamp_us": 1000300},
        {"kind": "first-body", "trace": "case-1", "timestamp_us": 1009500},
    ])
    item = result["p95_request"]
    assert item["spans_ms"]["client_before_syn"] == .2
    assert item["spans_ms"]["relay_tcp_handshake"] == .1
    assert item["spans_ms"]["client_connected_to_get"] == .5
    assert item["spans_ms"]["relay_kernel_to_handler"] == .2
    assert item["points_us"]["relay_kernel_synack"] == 1000300
    assert "client_to_relay_kernel" not in item["spans_ms"]
    assert sum(item["spans_ms"].values()) == 10


def test_fresh_client_handshake_cannot_be_silently_assumed_without_syn():
    row = sample()
    row["startup_hash_intervals"] = []
    with pytest.raises(AssertionError, match="TCP handshake"):
        attribute(row, origin_port=9010, relay_port=9020, downstream=True, wire_events=[
            {"kind": "request", "target_port": 9010, "trace": "case-1", "timestamp_us": 1002500},
            {"kind": "request", "source_port": 4000, "target_port": 9020,
             "trace": "case-1", "timestamp_us": 1000800},
            {"kind": "first-body", "trace": "case-1", "timestamp_us": 1009500},
        ])


@pytest.mark.skipif(sys.platform != "linux" or not binary_path().is_file(),
                    reason="requires the isolated Linux diagnostic environment")
@pytest.mark.requires_packet_capture
@pytest.mark.parametrize("mode", ["python", "rust"])
@pytest.mark.parametrize("collector", [FirstBodyWireProfile, ProcessFirstBodyWireProfile])
def test_kernel_first_body_and_original_consumer_keep_all_media_bytes(mode, collector):
    with experiment(trace=True, profile=True) as relays:
        url = relays[mode].url
        port = urlsplit(url).port
        with collector({port}, body_port=port, expected=4) as captured:
            result = asyncio.run(transfer_load(
                url + "/t4/media/file", concurrency=4, duration=0, size=8192,
                expected=hashlib.sha256(pattern(0, 8192)).hexdigest(),
                trace_prefix="downstream-integration", consumer_timing=True,
            ))
    assert result["requests"] == 4 and result["hashes_match"]
    assert captured.body_events == 4 and captured.dropped == 0
    assert "startup_hash_intervals" in result
    bodies = {e["trace"]: e["timestamp_us"] for e in captured.events if e["kind"] == "first-body"}
    for client in result["trace_samples"]:
        assert client["client_start_us"] < bodies[client["trace"]] <= client["client_first_body_us"]


@pytest.mark.skipif(sys.platform != "linux" or not binary_path().is_file(),
                    reason="requires the isolated Linux diagnostic environment")
@pytest.mark.requires_packet_capture
@pytest.mark.parametrize("mode", ["python", "rust"])
def test_shared_downstream_measurement_closes_original_request_timelines(mode):
    with experiment(trace=True, profile=True) as relays:
        result = measured_downstream_load(
            relays, mode, concurrency=4, duration=0, size=8192,
            expected=hashlib.sha256(pattern(0, 8192)).hexdigest(),
            consumer_mode="async", profile=True,
        )
    assert result["requests"] == result["attribution"]["matched"] == 4
    assert result["hashes_match"] and result["wire_dropped"] == 0
    assert all(sample["spans_ms"]["relay_to_downstream_kernel"] >= 0
               for sample in result["attribution"]["samples"])
    assert all(sample["spans_ms"]["relay_tcp_handshake"] >= 0
               and sample["spans_ms"]["client_before_syn"] >= 0
               and sample["spans_ms"]["client_connected_to_get"] >= 0
               and sample["spans_ms"]["relay_kernel_to_handler"] >= 0
               for sample in result["attribution"]["samples"])
