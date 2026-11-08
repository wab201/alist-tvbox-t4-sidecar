import asyncio
import hashlib
import json
import threading
import time

import pytest

from scripts.benchmark_rust_media import transfer_load
from scripts.diagnose_rust_endpoints import analyze_existing, paired_effects, summarize
from tests.rust.relay_support import binary_path, experiment, pattern


@pytest.mark.skipif(not binary_path().is_file(), reason="Rust release binary required")
@pytest.mark.parametrize("hash_offload", [False, True])
def test_hash_diagnostic_preserves_requests_bytes_and_complete_digest(monkeypatch, hash_offload):
    threads = []
    active = peak = 0
    lock = threading.Lock()
    original = asyncio.to_thread
    main = threading.get_ident()

    async def observed(function, *args):
        def run():
            nonlocal active, peak
            threads.append(threading.get_ident())
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.002)
                return function(*args)
            finally:
                with lock:
                    active -= 1
        return await original(run)

    monkeypatch.setattr(asyncio, "to_thread", observed)
    size = 8192
    with experiment() as relays:
        result = asyncio.run(transfer_load(
            relays["rust"].url + "/t4/media/file", concurrency=4, duration=.001,
            size=size, expected=hashlib.sha256(pattern(0, size)).hexdigest(),
            trace_prefix=f"endpoint-{time.time_ns()}", hash_offload=hash_offload,
        ))
    assert result["requests"] == 4
    assert result["bytes"] == size * 4
    assert result["hashes_match"]
    assert bool(threads) == hash_offload
    assert all(worker != main for worker in threads)
    assert peak == int(hash_offload)


def test_offloaded_hash_waits_are_not_mislabeled_as_synchronous_cpu_intervals():
    with pytest.raises(ValueError, match="offloaded work"):
        asyncio.run(transfer_load(
            "http://invalid.invalid", concurrency=1, duration=.001, size=1,
            expected="unused", trace_prefix="unused",
            hash_offload=True, consumer_timing=True,
        ))


def test_endpoint_summary_does_not_mix_source_and_consumer_conditions():
    rows = []
    for fair in (False, True):
        for offload in (False, True):
            for mode, latency in (("python", 20), ("rust", 25)):
                rows.append({
                    "fair_source": fair, "hash_offload": offload, "mode": mode,
                    "ttfb_p95_ms": latency + 100 * fair + 1000 * offload,
                    "origin_loop_lag_p95_ms": 1, "hashes_match": True,
                    "attribution": {
                        "mean_spans_ms": {"origin_headers_to_body": 2},
                        "samples": [{"spans_ms": {"origin_headers_to_body": 2}}],
                    },
                })
    result = summarize(rows)
    assert len(result) == 4
    for cell in result:
        assert cell["p95_difference_ms"] == 5
        assert cell["python_p95_ms"] == (
            20 + 100 * cell["fair_source"] + 1000 * cell["hash_offload"]
        )
        assert cell["mean_nodes_ms"]["rust"]["origin_headers_to_body"] == 2
        assert "passed" not in cell


def endpoint_rows():
    rows = []
    for repeat, (baseline, offload) in enumerate(((166, 82), (116, 126), (168, 193))):
        for mode in ("python", "rust"):
            for fair in (False, True):
                for moved in (False, True):
                    latency = offload if moved else baseline
                    rows.append({
                        "repeat": repeat, "mode": mode, "fair_source": fair,
                        "hash_offload": moved, "ttfb_p95_ms": latency - 20 * fair,
                        "mib_per_second": 50 if moved else 150, "hashes_match": True,
                    })
    return rows


def test_endpoint_paired_effects_do_not_turn_group_median_changes_into_repeatable_gains():
    effects = paired_effects(endpoint_rows())
    assert len(effects) == 8
    effect, = [value for value in effects
               if value["mode"] == "rust" and value["factor"] == "hash_offload"
               and value["fixed"] == {"fair_source": False}]
    assert effect["lower_p95_rounds"] == 1
    assert effect["median_paired_p95_change_ms"] == 10
    assert effect["median_paired_throughput_ratio"] == pytest.approx(1 / 3)
    assert [pair["p95_change_ms"] for pair in effect["pairs"]] == [-84, 10, 25]
    assert "passed" not in effect


@pytest.mark.parametrize("defect", ["missing", "duplicate", "unverified", "missing-round"])
def test_endpoint_paired_effects_reject_incomplete_or_unverified_evidence(defect):
    rows = endpoint_rows()
    if defect == "missing":
        rows.pop()
    elif defect == "duplicate":
        rows.append(rows[0])
    elif defect == "missing-round":
        rows = [row for row in rows if row["repeat"] != 2]
    else:
        rows[0]["hashes_match"] = False
    with pytest.raises(ValueError):
        paired_effects(rows)


def test_endpoint_offline_analysis_preserves_input_and_never_restarts_the_load(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline analysis must not start a load or locate a relay")

    monkeypatch.setattr("scripts.diagnose_rust_endpoints.experiment", forbidden)
    monkeypatch.setattr("scripts.diagnose_rust_endpoints.measured_load", forbidden)
    monkeypatch.setattr("scripts.diagnose_rust_endpoints.binary_path", forbidden)
    source = tmp_path / "input.json"
    source.write_text(json.dumps({
        "scope": "diagnostic-only-source-fairness-x-hash-offload",
        "audit_completed": True, "binary_sha256": "recorded-binary", "rows": endpoint_rows(),
    }), encoding="utf-8")
    original = source.read_bytes()
    output = tmp_path / "paired.json"
    result = analyze_existing(source, output)
    assert source.read_bytes() == original
    assert result["input"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert result["binary_sha256"] == "recorded-binary"
    assert result["production_ready"] is False
    assert result["performance_gate"] == "not-applied"
    assert json.loads(output.read_text()) == result
    with pytest.raises(FileExistsError):
        analyze_existing(source, output)
    with pytest.raises(ValueError, match="must not replace"):
        analyze_existing(source, source)
    assert source.read_bytes() == original


@pytest.mark.parametrize("invalid", [
    {"scope": "other-report", "audit_completed": True},
    {"scope": "diagnostic-only-source-fairness-x-hash-offload", "audit_completed": False},
])
def test_endpoint_offline_analysis_rejects_wrong_or_incomplete_reports(tmp_path, invalid):
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(invalid), encoding="utf-8")
    output = tmp_path / "unused.json"
    with pytest.raises(ValueError, match="completed endpoint"):
        analyze_existing(source, output)
    assert not output.exists()
