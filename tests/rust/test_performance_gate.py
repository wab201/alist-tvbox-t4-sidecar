from scripts.benchmark_rust_media import gate


def samples(*, rust_throughput=110, rust_cpu=7, rust_rss=70):
    rows = []
    for concurrency in (1, 4, 8, 16):
        for _ in range(3):
            for mode, throughput, cpu, rss in (
                ("python", 100, 10, 100), ("rust", rust_throughput, rust_cpu, rust_rss),
            ):
                rows.append({
                    "concurrency": concurrency, "mode": mode,
                    "mib_per_second": throughput, "cpu_seconds_per_gib": cpu,
                    "rss_peak": rss, "ttfb_p95_ms": 10, "hashes_match": True,
                })
    return rows


def test_gate_requires_compatibility_and_repeated_complete_evidence():
    assert gate(samples(), contracts_passed=True, repeats=3)["passed"]
    assert not gate(samples(), contracts_passed=False, repeats=3)["passed"]
    assert not gate(samples(), contracts_passed=True, repeats=4)["passed"]
    assert not gate(samples(), contracts_passed=True, repeats=2)["passed"]
    assert not gate(samples()[:6], contracts_passed=True, repeats=3)["passed"]


def test_speed_without_cpu_or_memory_benefit_is_not_accepted():
    assert not gate(samples(rust_cpu=9), contracts_passed=True, repeats=3)["passed"]
    assert not gate(samples(rust_rss=90), contracts_passed=True, repeats=3)["passed"]
    assert not gate(samples(rust_throughput=94), contracts_passed=True, repeats=3)["passed"]


def test_latency_regression_is_not_hidden_by_throughput():
    rows = samples()
    for row in rows:
        if row["mode"] == "rust":
            row["ttfb_p95_ms"] = 20
    assert not gate(rows, contracts_passed=True, repeats=3)["passed"]


def test_exact_threshold_is_accepted_but_missing_measurements_are_not():
    assert gate(samples(rust_cpu=8, rust_rss=85, rust_throughput=95),
                contracts_passed=True, repeats=3)["passed"]
    rows = samples()
    rows[0]["cpu_seconds_per_gib"] = 0
    rows[2]["cpu_seconds_per_gib"] = 0
    rows[4]["cpu_seconds_per_gib"] = 0
    assert not gate(rows, contracts_passed=True, repeats=3)["passed"]
    rows = samples()
    rows[0]["hashes_match"] = False
    assert not gate(rows, contracts_passed=True, repeats=3)["passed"]
