import asyncio
import json
import socket
import time
from urllib.parse import urlsplit

import httpx
import psutil
import pytest

from tests.rust.relay_support import HEADERS, binary_path, experiment, pattern

pytestmark = pytest.mark.skipif(
    not binary_path().is_file(), reason="experimental Rust binary not built",
)


def target(relays, key):
    return relays["rust"].url + "/t4/media/" + key


def health(relays):
    return httpx.get(relays["rust"].url + "/health", trust_env=False).json()


def logs(relays):
    return [json.loads(line) for line in relays["rust"].log.read_text(encoding="utf-8").splitlines()
            if '"event":"relay.closed"' in line]


@pytest.mark.parametrize("case", ["headers", "body", "primary-error", "truncated"])
def test_delayed_hedge_chooses_successful_body_and_cancels_loser(case):
    with experiment(hedge_delay_ms=40, trace=True) as relays:
        with httpx.Client(trust_env=False, timeout=3) as client:
            response = client.get(target(relays, "hedge-" + case),
                                  headers={"Range": "bytes=0-8191"})
            assert response.status_code == 206
            assert response.content == pattern(0, 8192)
            deadline = time.monotonic() + 2
            while True:
                state = client.get(relays["origin"].url + "/health").json()
                stats = health(relays)
                if not state["active"] and not state["hedge_header_active"] and not stats["active"]:
                    break
                assert time.monotonic() < deadline
                time.sleep(.01)
        assert stats["hedge_started"] == stats["hedge_wins"] == 1
        assert stats["hedge_active"] == 0
        assert list(state["hedge_attempts"].values()) == [2]
        assert state["generated_bytes"] < 2 * 1024 ** 2
        assert logs(relays)[-1]["hedge_winner"] == 1


@pytest.mark.parametrize("key", ["backup-error", "backup-empty"])
def test_fast_error_does_not_beat_slow_valid_primary_and_original_error_is_preserved(key):
    with experiment(hedge_delay_ms=40) as relays, httpx.Client(trust_env=False, timeout=3) as client:
        response = client.get(target(relays, "hedge-" + key),
                              headers={"Range": "bytes=0-8191"})
        assert response.status_code == 206 and response.content == pattern(0, 8192)
        response = client.get(target(relays, "hedge-both-error"),
                              headers={"Range": "bytes=0-8191"})
        assert response.status_code == 500 and response.content == b"fixture-error"
        assert health(relays)["hedge_wins"] == 0


def test_head_and_unapproved_assets_never_duplicate_even_with_short_delay():
    with experiment(hedge_delay_ms=1) as relays, httpx.Client(trust_env=False, timeout=3) as client:
        assert client.head(target(relays, "hedge-headers")).status_code == 200
        assert client.get(target(relays, "file"), headers={
            "Range": "bytes=0-8191",
        }).content == pattern(0, 8192)
        assert health(relays)["hedge_started"] == 0
        state = client.get(relays["origin"].url + "/health").json()
        assert all(value == 1 for value in state["hedge_attempts"].values())


def test_response_ready_before_delay_does_not_start_backup():
    with experiment(hedge_delay_ms=1000) as relays, httpx.Client(trust_env=False, timeout=3) as client:
        response = client.get(target(relays, "hedge-fast"), headers={"Range": "bytes=0-8191"})
        assert response.content == pattern(0, 8192)
        assert health(relays)["hedge_started"] == 0
        state = client.get(relays["origin"].url + "/health").json()
        assert list(state["hedge_attempts"].values()) == [1]


def test_default_keeps_original_single_request_behavior():
    with experiment() as relays, httpx.Client(trust_env=False, timeout=3) as client:
        response = client.get(target(relays, "hedge-headers"),
                              headers={"Range": "bytes=0-8191"})
        assert response.content == pattern(0, 8192)
        state = client.get(relays["origin"].url + "/health").json()
        assert list(state["hedge_attempts"].values()) == [1]
        assert health(relays)["hedge_started"] == 0


def test_range_headers_and_large_seek_are_identical_on_both_attempts():
    with experiment(hedge_delay_ms=40) as relays, httpx.Client(trust_env=False, timeout=3) as client:
        response = client.get(target(relays, "hedge-headers"), headers={
            "Range": "bytes=11939-20130", "If-Range": '"fixture-generation-2"',
        })
        assert response.status_code == 206 and response.content == pattern(11939, 8192)
        seen = client.get(relays["origin"].url + "/health").json()["seen"]
        calls = [row for row in seen if row["path"] == "hedge/headers"]
        assert len(calls) == 2
        for row in calls:
            assert row["range"] == "bytes=11939-20130"
            assert row["if-range"] == '"fixture-generation-2"'
            assert row["referer"] == HEADERS["Referer"]
            assert row["authorization"] == HEADERS["Authorization"]
            assert row["cookie"] == HEADERS["Cookie"]


def test_extra_concurrency_budget_is_bounded_and_not_queued():
    with experiment(hedge_delay_ms=30, hedge_limit=1) as relays:
        async def consume():
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                return await asyncio.gather(*(
                    client.get(target(relays, "hedge-both-slow"), headers={
                        "Range": f"bytes={index * 8192}-{(index + 1) * 8192 - 1}",
                    }) for index in range(8)
                ))
        responses = asyncio.run(consume())
        assert all(response.status_code == 206 for response in responses)
        for index, response in enumerate(responses):
            assert response.content == pattern(index * 8192, 8192)
        stats = health(relays)
        assert stats["hedge_peak"] == stats["hedge_started"] == 1
        assert stats["hedge_skipped"] == 7
        assert stats["hedge_active"] == 0


def test_client_disconnect_cancels_both_attempts_and_returns_budget():
    with experiment(hedge_delay_ms=30, hedge_limit=1) as relays:
        parsed = urlsplit(target(relays, "hedge-both-slow"))
        sock = socket.create_connection((parsed.hostname, parsed.port))
        try:
            sock.sendall(
                f"GET {parsed.path} HTTP/1.1\r\nHost: localhost\r\nRange: bytes=0-8191\r\n\r\n".encode()
            )
            deadline = time.monotonic() + 2
            while health(relays)["hedge_active"] != 1:
                assert time.monotonic() < deadline
                time.sleep(.01)
        finally:
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        deadline = time.monotonic() + 2
        while True:
            stats = health(relays)
            if stats["hedge_active"] == stats["active"] == 0:
                break
            assert time.monotonic() < deadline
            time.sleep(.01)


def test_hedged_large_object_stays_bounded_under_backpressure_and_disconnect():
    with experiment(hedge_delay_ms=40) as relays, httpx.Client(trust_env=False, timeout=3) as client:
        process = psutil.Process(relays["rust"].process.pid)
        baseline = peak = process.memory_info().rss
        with client.stream("GET", target(relays, "hedge-large")) as response:
            assert response.status_code == 200
            assert int(response.headers["content-length"]) == 5 * 1024 ** 3
            for index, chunk in zip(range(80), response.iter_raw(4096)):
                assert chunk == pattern(index * 4096, 4096)
                peak = max(peak, process.memory_info().rss)
                time.sleep(.01)
        deadline = time.monotonic() + 2
        while True:
            stats = health(relays)
            source = client.get(relays["origin"].url + "/health").json()
            if not any((stats["active"], stats["hedge_active"], source["active"],
                        source["hedge_header_active"])):
                break
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert stats["hedge_wins"] == 1
        assert list(source["hedge_attempts"].values()) == [2]
        assert peak - baseline < 32 * 1024 ** 2
        assert source["generated_bytes"] < 32 * 1024 ** 2


def test_unhelpful_race_cools_only_its_asset_and_recovers_after_cooldown():
    with experiment(hedge_delay_ms=40) as relays, httpx.Client(trust_env=False, timeout=3) as client:
        for index in range(2):
            response = client.get(target(relays, "hedge-both-slow"), headers={
                "Range": f"bytes={index * 8192}-{(index + 1) * 8192 - 1}",
            })
            assert response.content == pattern(index * 8192, 8192)
        stats = health(relays)
        assert stats["hedge_started"] == 1
        assert stats["hedge_cooldown_skipped"] == 1
        response = client.get(target(relays, "hedge-body"), headers={"Range": "bytes=0-8191"})
        assert response.content == pattern(0, 8192)
        assert health(relays)["hedge_wins"] == 1
        for _ in range(8):
            assert client.get(target(relays, "hedge-fast"), headers={
                "Range": "bytes=0-8191",
            }).content == pattern(0, 8192)
        time.sleep(1.02)
        response = client.get(target(relays, "hedge-both-slow"), headers={"Range": "bytes=16384-24575"})
        assert response.content == pattern(16384, 8192)
        stats = health(relays)
        assert stats["hedge_started"] == 3 and stats["hedge_active"] == 0


def test_hedge_credit_exhaustion_never_blocks_primary_and_replenishes():
    with experiment(hedge_delay_ms=40) as relays, httpx.Client(trust_env=False, timeout=3) as client:
        for index in range(3):
            response = client.get(target(relays, "hedge-headers"), headers={
                "Range": f"bytes={index * 8192}-{(index + 1) * 8192 - 1}",
            })
            assert response.content == pattern(index * 8192, 8192)
        stats = health(relays)
        assert stats["hedge_started"] == stats["hedge_wins"] == 2
        assert stats["hedge_budget_skipped"] == 1 and stats["hedge_active"] == 0
        source = client.get(relays["origin"].url + "/health").json()
        assert sorted(source["hedge_attempts"].values()) == [1, 2, 2]
        for _ in range(8):
            assert client.get(target(relays, "hedge-fast"), headers={
                "Range": "bytes=0-8191",
            }).content == pattern(0, 8192)
        response = client.get(target(relays, "hedge-headers"), headers={
            "Range": "bytes=24576-32767",
        })
        assert response.content == pattern(24576, 8192)
        assert health(relays)["hedge_wins"] == 3


def test_no_credit_delivers_headers_before_a_gated_body_without_prefetching():
    with experiment(hedge_delay_ms=40) as relays, httpx.Client(trust_env=False, timeout=3) as control:
        for index in range(2):
            response = control.get(target(relays, "hedge-headers"), headers={
                "Range": f"bytes={index * 8192}-{(index + 1) * 8192 - 1}",
            })
            assert response.content == pattern(index * 8192, 8192)
        assert health(relays)["hedge_credits"] == 0

        async def consume():
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                request = client.build_request("GET", target(relays, "hedge-gated-body"),
                                              headers={"Range": "bytes=0-8191"})
                try:
                    # No source body can exist until the response headers arrive.
                    response = await asyncio.wait_for(client.send(request, stream=True), 2)
                    assert response.status_code == 206
                    await client.post(relays["origin"].url + "/__release_body")
                    assert await response.aread() == pattern(0, 8192)
                    await response.aclose()
                finally:
                    await client.post(relays["origin"].url + "/__release_body")

        asyncio.run(consume())
        stats = health(relays)
        assert stats["hedge_started"] == 2 and stats["hedge_budget_skipped"] == 1
        assert stats["active"] == stats["hedge_active"] == 0


def test_strongly_validated_headers_do_not_wait_for_the_first_body():
    with experiment(hedge_delay_ms=1000) as relays:
        async def consume():
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                request = client.build_request("GET", target(relays, "hedge-gated-body"),
                                              headers={"Range": "bytes=0-8191"})
                try:
                    response = await asyncio.wait_for(client.send(request, stream=True), 2)
                    assert response.status_code == 206
                    assert response.headers["etag"] == '"fixture-generation-2"'
                    await client.post(relays["origin"].url + "/__release_body")
                    assert await response.aread() == pattern(0, 8192)
                    await response.aclose()
                finally:
                    await client.post(relays["origin"].url + "/__release_body")
        asyncio.run(consume())


@pytest.mark.parametrize("case", ["body-no-etag", "body-weak-etag"])
def test_without_a_strong_validator_existing_first_body_selection_is_preserved(case):
    with experiment(hedge_delay_ms=40) as relays, httpx.Client(trust_env=False, timeout=3) as client:
        response = client.get(target(relays, "hedge-" + case), headers={"Range": "bytes=0-8191"})
        assert response.status_code == 206 and response.content == pattern(0, 8192)
        assert health(relays)["hedge_wins"] == 1


@pytest.mark.parametrize("case", ["body-changed-etag", "body-changed-range"])
def test_after_early_headers_a_different_backup_contract_is_never_delivered(case):
    with experiment(hedge_delay_ms=1000) as relays:
        async def consume():
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                request = client.build_request("GET", target(relays, "hedge-" + case),
                                              headers={"Range": "bytes=0-8191"})
                try:
                    response = await asyncio.wait_for(client.send(request, stream=True), 3)
                    assert response.status_code == 206
                    assert response.headers["etag"] == '"fixture-generation-2"'
                    assert response.headers["content-range"].startswith("bytes 0-8191/")
                    deadline = time.monotonic() + 4
                    while True:
                        state = (await client.get(relays["rust"].url + "/health")).json()
                        if state["hedge_rejected"] == 1:
                            break
                        assert time.monotonic() < deadline
                        await asyncio.sleep(.01)
                    await client.post(relays["origin"].url + "/__release_body")
                    assert await response.aread() == pattern(0, 8192)
                    await response.aclose()
                finally:
                    await client.post(relays["origin"].url + "/__release_body")
        asyncio.run(consume())
        state = health(relays)
        assert state["hedge_started"] == state["hedge_rejected"] == 1
        assert state["hedge_wins"] == 0 and state["hedge_active"] == 0
