import pytest

from scripts.diagnose_rust_calibrated import sched_delta, summarize, validate_connections


def test_reuse_requires_observed_upstream_socket():
    samples = [{"trace": "a"}]
    assert validate_connections(samples, [{"trace": "a", "peer_port": 123}],
                                phase="warm", previous_ports=[123]) == [123]
    assert samples[0]["confirmed_reused"] is True
    with pytest.raises(ValueError, match="unprimed"):
        validate_connections(samples, [{"trace": "a", "peer_port": 124}],
                             phase="warm", previous_ports=[123])


def test_cold_cannot_relabel_reused_connection():
    with pytest.raises(ValueError, match="cold"):
        validate_connections([{"trace": "a"}], [{"trace": "a", "peer_port": 123}],
                             phase="cold", previous_ports=[123])
    with pytest.raises(ValueError, match="missing"):
        validate_connections([{"trace": "missing"}], [], phase="warm")


def test_scheduler_delta_is_per_surviving_thread():
    assert sched_delta({"1": {"run_ns": 10, "wait_ns": 20, "slices": 2}},
                       {"1": {"run_ns": 30, "wait_ns": 50, "slices": 5},
                        "2": {"run_ns": 5}}) == {
        "1": {"run_ns": 20, "wait_ns": 30, "slices": 3}}


def test_calibrated_summary_keeps_cold_and_warm_separate():
    rows = [{"phase": phase, "concurrency": 16, "repeat": repeat, "mode": mode,
             "hashes_match": True, "connection_verified": True,
             "ttfb_p95_ms": 20 + (10 if mode == "rust" and phase == "cold" else 0),
             "mib_per_second": 100}
            for phase in ("cold", "warm") for repeat in range(3)
            for mode in ("origin", "python", "rust")]
    summary = summarize(rows)
    assert [row["median_change_ms"] for row in summary] == [10, 0]
    with pytest.raises(AssertionError):
        summarize(rows[:-1])
