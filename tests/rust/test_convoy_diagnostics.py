import pytest

from scripts.diagnose_rust_convoy import attribute, summarize


def sample():
    return {
        "requests": 1,
        "profile_raw": {
            "clients": [{
                "trace": "case-1", "client_start_us": 1000000,
                "client_first_body_us": 1010000, "ttfb_ms": 10, "request_number": 0,
            }],
            "origins": [{
                "trace": "case-1", "handler_us": 1003000, "response_headers_us": 1004000,
                "first_body_us": 1008000,
            }],
            "relays": [{
                "client_trace": "case-1", "handler_us": 1001000,
                "upstream_request_us": 1002000, "first_body_us": 1009000,
            }],
        },
    }


def test_same_request_continuous_stages_close_without_adding_overlapping_waits():
    result = attribute(sample())
    assert result["matched"] == 1
    assert result["p95_request"]["spans_ms"]["origin_headers_to_body"] == 4
    assert sum(result["p95_request"]["spans_ms"].values()) == 10
    assert result["p95_request"]["closure_error_ms"] == 0


def test_missing_or_invalid_timelines_cannot_silently_pass():
    row = sample()
    row["requests"] = 2
    with pytest.raises(AssertionError, match="missing"):
        attribute(row)
    row = sample()
    row["profile_raw"]["origins"][0]["first_body_us"] = 1000000
    with pytest.raises(AssertionError, match="clock"):
        attribute(row)


def test_kernel_timestamps_split_dispatch_and_server_queue_for_same_request():
    result = attribute(sample(), wire_events=[{
        "kind": "request", "target_port": 9010, "trace": "case-1", "timestamp_us": 1002500,
    }], origin_port=9010)
    spans = result["p95_request"]["spans_ms"]
    assert spans["dispatch_to_origin_kernel"] == .5
    assert spans["origin_kernel_to_handler"] == .5
    assert "dispatch_to_origin_handler" not in spans
    assert sum(spans.values()) == 10
    with pytest.raises(AssertionError, match="kernel request"):
        attribute(sample(), wire_events=[], origin_port=9010)


def test_unprofiled_summary_keeps_original_latency_budget_and_resource_comparison():
    rows = [
        {
            "fair_source": False, "mode": mode, "ttfb_p95_ms": latency,
            "mib_per_second": speed, "cpu_seconds_per_gib": cpu, "rss_peak": rss,
            "requests": 16, "hashes_match": True,
        }
        for _ in range(4)
        for mode, latency, speed, cpu, rss in (
            ("python", 120, 100, 20, 100),
            ("rust", 134, 120, 5, 25),
        )
    ]
    result, = summarize(rows)
    assert result["rounds"] == 4
    assert result["unchanged_limit_ms"] == 12
    assert result["ttfb_delta_ms"] == 14
    assert not result["latency_within_budget"]
    assert result["throughput_ratio"] == 1.2
    assert result["cpu_reduction"] == result["rss_reduction"] == .75
    assert result["requests"] == {"python": 64, "rust": 64}
    assert result["hashes_match"]
    assert "mean_node_ms" not in result
