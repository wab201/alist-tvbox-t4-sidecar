import asyncio
import json

import pytest

from scripts.diagnose_rust_fastpath import measure
from tests.rust.relay_support import binary_path, experiment

pytestmark = pytest.mark.skipif(
    not binary_path().is_file(), reason="experimental Rust binary not built",
)


def test_fastpath_diagnostic_uses_independent_connections_and_pairs_complete_hashes():
    with experiment(profile=True) as relays:
        row = asyncio.run(measure(
            relays, mode="single", consumer="sampled", size=8192, concurrency=2,
            batches=2, trace=True, prefix="diagnostic-contract", connections="per-worker",
        ))
        assert row["hashes_match"] and row["bytes"] == 8192 * 6
        assert row["consumer_connection_count"] == 2
        assert row["paired"]["matched"] == row["warm_samples"] == 4
        assert row["paired"]["negative_clock_samples"] == 0
        assert row["extra_requests"] == 0
        assert row["rss_calls"] >= 1
        for sample in row["paired"]["samples"]:
            assert sample["points_us"]["client_send_start"] is not None
        assert "authorization" not in json.dumps(row).lower()
