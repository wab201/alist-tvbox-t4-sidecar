import pytest

from scripts.diagnose_rust_affinity import paired_effects, verify_masks


def test_legacy_mask_does_not_count_as_whole_thread_isolation():
    masks = {10: [11], 11: [8, 9, 10, 11], 12: [8, 9, 10, 11]}
    verify_masks(masks, 10, [11], [8, 9, 10, 11], "leader-only")
    with pytest.raises(AssertionError, match="budget"):
        verify_masks(masks, 10, [11], [8, 9, 10, 11], "all-threads")


def test_new_thread_must_remain_in_budget():
    verify_masks({10: [11], 11: [11], 12: [11]}, 10, [11], [8, 9, 10, 11], "all-threads")
    with pytest.raises(AssertionError, match="leader"):
        verify_masks({11: [11]}, 10, [11], [8, 9, 10, 11], "all-threads")


def fixture_rows():
    return [{"concurrency": 16, "mode": mode, "repeat": repeat,
             "condition": condition, "hashes_match": True,
             "ttfb_p95_ms": (166, 116, 168)[repeat] if condition == "leader-only" else (82, 126, 193)[repeat],
             "mib_per_second": 150 if condition == "leader-only" else 50}
            for mode in ("origin", "python", "rust") for repeat in range(3)
            for condition in ("leader-only", "all-threads")]


def test_paired_effects_preserve_same_round_regressions():
    result = paired_effects(fixture_rows())
    assert all(row["improved_rounds"] == 1 for row in result)
    assert all(row["median_p95_change_ms"] == 10 for row in result)
    assert all(row["median_throughput_ratio"] == pytest.approx(1 / 3) for row in result)


@pytest.mark.parametrize("failure", ["duplicate", "missing", "unverified"])
def test_incomplete_evidence_cannot_be_summarized(failure):
    rows = fixture_rows()
    if failure == "duplicate":
        rows.append(rows[0])
    elif failure == "missing":
        rows.pop()
    else:
        rows[0]["hashes_match"] = False
    with pytest.raises(AssertionError):
        paired_effects(rows)
