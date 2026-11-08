import concurrent.futures
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

import httpx
import psutil
import pytest

from tests.rust.relay_support import (
    CIPHERTEXT, ETAG, HEADERS, KEY, MODIFIED, binary_path, experiment, pattern,
)

pytestmark = pytest.mark.skipif(
    not binary_path().is_file(), reason="experimental Rust release binary has not been built",
)


@pytest.mark.parametrize("flag", ["--frame-max-kib", "--upstream-dispatch-us"])
def test_removed_experiment_options_are_rejected_by_actual_binary(flag):
    result = subprocess.run(
        [str(binary_path()), flag, "64"], capture_output=True, timeout=5,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == b"unknown argument"


@pytest.fixture(scope="module",
                params=["single", "hedged", "budgeted", "adaptive", "limited", "startup", "async-log"])
def relays(request):
    with experiment(hedge_delay_ms=40 if request.param == "hedged" else None,
                    approve_all_assets=request.param == "hedged",
                    delivery_budget_kib=256 if request.param == "budgeted" else None,
                    upstream_read_kib={"adaptive": 0, "limited": 64}.get(request.param),
                    startup_fairness=request.param == "startup",
                    relay_args=["--async-log"] if request.param == "async-log" else []) as instances:
        yield instances


def address(relays, mode, key):
    return relays[mode].url + "/t4/media/" + key


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("offset", [0, 5 * 1024 ** 3 // 2, 5 * 1024 ** 3 - 8192])
def test_range_large_offsets_headers_and_bytes_match_python(relays, method, offset):
    results = []
    with httpx.Client(trust_env=False) as client:
        for mode in ("python", "rust"):
            response = client.request(method, address(relays, mode, "large"), headers={
                "Range": f"bytes={offset}-{offset + 8191}", "If-Range": ETAG,
                "Authorization": "must-not-replace-asset-header",
                "Referer": "https://must-not-replace.example",
            })
            assert response.status_code == 206
            assert response.content == (pattern(offset, 8192) if method == "GET" else b"")
            assert response.headers["Content-Length"] == "8192"
            assert response.headers["Content-Range"] == f"bytes {offset}-{offset + 8191}/{5 * 1024 ** 3}"
            assert response.headers["ETag"] == ETAG
            assert response.headers["Last-Modified"] == MODIFIED
            results.append((response.status_code, response.content, dict(response.headers)))
        seen = client.get(relays["origin"].url + "/health").json()["seen"][-2:]
        for request in seen:
            assert request["referer"] == HEADERS["Referer"]
            assert request["authorization"] == HEADERS["Authorization"]
            assert request["cookie"] == HEADERS["Cookie"]
            assert request["user-agent"] == HEADERS["User-Agent"]
            assert request["accept-encoding"] == "identity"
            assert request["method"] == method
            assert request["if-range"] == ETAG
    for name in ("content-type", "content-length", "content-range", "accept-ranges", "etag",
                 "last-modified", "cache-control", "x-accel-buffering"):
        assert results[0][2][name] == results[1][2][name]


@pytest.mark.parametrize("mode", ["python", "rust"])
def test_suffix_open_ended_unsatisfied_and_stale_ranges(relays, mode):
    total = 5 * 1024 ** 3
    target = address(relays, mode, "large")
    with httpx.Client(trust_env=False) as client:
        for value in (f"bytes={total - 8192}-", "bytes=-8192"):
            response = client.get(target, headers={"Range": value, "If-Range": MODIFIED})
            assert response.status_code == 206
            assert response.content == pattern(total - 8192, 8192)
        response = client.get(target, headers={"Range": f"bytes={total}-"})
        assert response.status_code == 416
        assert response.headers["Content-Range"] == f"bytes */{total}"
        with client.stream("GET", target, headers={"Range": "bytes=100-200", "If-Range": '"stale"'}) as response:
            assert response.status_code == 200
            assert "Content-Range" not in response.headers
            assert response.headers["Content-Length"] == str(total)
            assert next(response.iter_raw(8192)) == pattern(0, 8192)
        response = client.head(target)
        assert response.status_code == 200 and response.content == b""
        assert response.headers["Content-Length"] == str(total)


@pytest.mark.parametrize("mode", ["python", "rust"])
def test_chunked_or_ignored_range_does_not_invent_range_metadata(relays, mode):
    with httpx.Client(trust_env=False) as client:
        response = client.get(address(relays, mode, "chunked"),
                              headers={"Range": "bytes=0-8191"})
        assert response.status_code == 206 and response.content == pattern(0, 8192)
        assert "Content-Length" not in response.headers
        with client.stream("GET", address(relays, mode, "ignore-range"),
                           headers={"Range": "bytes=1024-2047"}) as response:
            assert response.status_code == 200
            assert "Content-Range" not in response.headers
            assert next(response.iter_raw(8192)) == pattern(0, 8192)


@pytest.mark.parametrize("key", ["key", "encrypted", "manifest", "encoded"])
def test_raw_body_mime_and_encoding_are_not_transformed(relays, key):
    results = []
    with httpx.Client(trust_env=False) as client:
        for mode in ("python", "rust"):
            with client.stream("GET", address(relays, mode, key)) as response:
                results.append((response.status_code, dict(response.headers),
                                b"".join(response.iter_raw())))
    assert results[0][0] == results[1][0] == 200
    assert results[0][2] == results[1][2]
    for name in ("content-type", "content-length", "content-encoding"):
        assert results[0][1].get(name) == results[1][1].get(name)
    if key == "key":
        assert results[1][2] == KEY
    elif key == "encrypted":
        assert results[1][2] == CIPHERTEXT
    elif key == "manifest":
        # Byte passthrough only; this does not test/implement HLS URL rewriting.
        assert b"segment.ts\n" in results[1][2]
    else:
        assert results[1][1]["content-encoding"] == "gzip"
        assert results[1][2].startswith(b"\x1f\x8b")


def test_signed_url_path_query_order_and_encoding_are_preserved(relays):
    with httpx.Client(trust_env=False) as client:
        for mode in ("python", "rust"):
            response = client.get(address(relays, mode, "signed"), headers={"Range": "bytes=0-8191"})
            assert response.status_code == 206 and response.content == pattern(0, 8192)
            seen = client.get(relays["origin"].url + "/health").json()["seen"][-1]
            assert seen["raw_path"] == "/folder/space%20file.mkv"
            assert seen["query"] == "signature=a%2FB%2b&part=02&empty=&part=01"


@pytest.mark.parametrize("key", ["redirect", "cross", "403", "404", "500", "unavailable", "loop"])
def test_redirect_cookie_authorization_and_errors_match_python(relays, key):
    with httpx.Client(trust_env=False, timeout=10) as client:
        responses = [client.get(address(relays, mode, key), headers={"Range": "bytes=0-8191"})
                     for mode in ("python", "rust")]
    assert responses[0].status_code == responses[1].status_code
    if key in {"redirect", "cross"}:
        assert responses[1].status_code == 206
        assert responses[0].content == responses[1].content == pattern(0, 8192)
    elif key in {"unavailable", "loop"}:
        assert responses[1].status_code == 502
        assert b"127.0.0.1" not in responses[1].content
    else:
        assert responses[0].content == responses[1].content == b"fixture-error"


@pytest.mark.parametrize("mode", ["python", "rust"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_upstream_throttle_is_forwarded_without_retry(relays, mode, method):
    with httpx.Client(trust_env=False, timeout=10) as client:
        def attempts():
            return sum(row["path"] == "status/429"
                       for row in client.get(relays["origin"].url + "/health").json()["seen"])
        before = attempts()
        response = client.request(method, address(relays, mode, "429"))
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "17"
        assert response.content == (b"fixture-error" if method == "GET" else b"")
        assert attempts() == before + 1


@pytest.mark.parametrize("mode", ["python", "rust"])
@pytest.mark.parametrize("key", ["stall-headers", "stall-body", "slow"])
def test_real_disconnect_cancels_upstream_and_releases_resources(relays, mode, key):
    parsed = urlsplit(address(relays, mode, key))
    with httpx.Client(trust_env=False) as probe:
        origin = relays["origin"].url + "/health"
        deadline = time.monotonic() + 3
        while probe.get(origin).json()["active"] and time.monotonic() < deadline:
            time.sleep(.01)
        before = probe.get(origin).json()["cancelled"]
        sock = socket.create_connection((parsed.hostname, parsed.port))
        sock.settimeout(3)
        try:
            sock.sendall(f"GET {parsed.path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            deadline = time.monotonic() + 3
            while not probe.get(origin).json()["active"] and time.monotonic() < deadline:
                time.sleep(.01)
            assert probe.get(origin).json()["active"] > 0
            if key != "stall-headers":
                assert sock.recv(65536)
        finally:
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = probe.get(origin).json()
            relay = probe.get(relays["rust"].url + "/health").json()
            if state["active"] == 0 and (mode != "rust" or relay["active"] == 0):
                break
            time.sleep(.01)
        assert state["active"] == 0, "downstream disconnected but upstream is still running"
        assert state["cancelled"] > before
        if mode == "rust":
            assert relay["active"] == 0
            assert relay["cancelled"] > 0


@pytest.mark.parametrize("mode", ["python", "rust"])
def test_truncated_body_is_an_error_not_silent_success(relays, mode):
    with httpx.Client(trust_env=False) as client:
        with pytest.raises(httpx.HTTPError):
            client.get(address(relays, mode, "truncated"))


@pytest.mark.parametrize("mode", ["python", "rust"])
def test_concurrent_seeks_do_not_mix_sessions(relays, mode):
    def seek(index):
        offset = index * 11939
        response = httpx.get(address(relays, mode, "large"), trust_env=False, headers={
            "Range": f"bytes={offset}-{offset + 8191}",
        })
        assert response.status_code == 206
        assert response.content == pattern(offset, 8192)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(seek, range(32)))


def test_rust_does_not_offer_arbitrary_url_proxy_or_write_methods(relays):
    with httpx.Client(trust_env=False) as client:
        assert client.get(address(relays, "rust", "not-registered"),
                          params={"url": relays["origin"].url}).status_code == 404
        for _ in range(20):
            response = client.post(address(relays, "rust", "file"), content=b"not-a-media-method")
            assert response.status_code == 405
            assert response.headers["allow"] == "GET, HEAD"


def test_slow_consumer_does_not_buffer_a_large_object(relays):
    process = psutil.Process(relays["rust"].process.pid)
    before = process.memory_info().rss
    peak = before
    with httpx.Client(trust_env=False) as client:
        with client.stream("GET", address(relays, "rust", "large")) as response:
            assert response.headers["Content-Length"] == str(5 * 1024 ** 3)
            for _, chunk in zip(range(80), response.iter_raw(4096)):
                assert chunk
                peak = max(peak, process.memory_info().rss)
                time.sleep(.01)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if client.get(relays["rust"].url + "/health").json()["active"] == 0:
                break
            time.sleep(.01)
        assert client.get(relays["rust"].url + "/health").json()["active"] == 0
    assert peak - before < 32 * 1024 ** 2


@pytest.mark.parametrize("upstream_read_kib", [None, 0, 64])
def test_read_idle_timeout_does_not_turn_a_partial_body_into_success(upstream_read_kib):
    with experiment(read_timeout_ms=400, upstream_read_kib=upstream_read_kib) as relays, \
            httpx.Client(trust_env=False, timeout=3) as client:
        response = client.get(address(relays, "rust", "stall-headers"))
        assert response.status_code == 502
        with pytest.raises(httpx.HTTPError):
            client.get(address(relays, "rust", "stall-body"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            health = client.get(relays["rust"].url + "/health").json()
            if health["active"] == 0:
                break
            time.sleep(.01)
        assert health["active"] == 0 and health["errors"] >= 2


@pytest.mark.parametrize("limit", [0, 8, 64])
def test_controlled_transport_limits_actual_reads_without_extra_requests(limit):
    import hashlib

    from scripts.benchmark_rust_media import expected_hash

    size = 8 * 1024 ** 2
    with experiment(upstream_read_kib=limit) as relays, \
            httpx.Client(trust_env=False, timeout=10) as client:
        def attempts():
            return len(client.get(relays["origin"].url + "/health").json()["seen"])
        before = attempts()
        digest, received = hashlib.sha256(), 0
        with client.stream("GET", address(relays, "rust", "file"),
                           headers={"Range": f"bytes=0-{size - 1}"}) as response:
            assert response.status_code == 206
            for chunk in response.iter_raw():
                digest.update(chunk)
                received += len(chunk)
        assert received == size and digest.hexdigest() == expected_hash(size)
        assert attempts() == before + 1
        health = client.get(relays["rust"].url + "/health").json()
        assert health["max_upstream_read"] > 0
        if limit:
            assert health["max_upstream_read"] <= limit * 1024


def test_stalled_startup_does_not_block_other_streams_and_cancel_releases_priority():
    with experiment(startup_fairness=True) as relays, \
            httpx.Client(trust_env=False, timeout=5) as client:
        parsed = urlsplit(address(relays, "rust", "stall-headers"))
        sock = socket.create_connection((parsed.hostname, parsed.port))
        try:
            sock.sendall(f"GET {parsed.path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            deadline = time.monotonic() + 3
            while True:
                health = client.get(relays["rust"].url + "/health").json()
                if health["starting"] == 1:
                    break
                assert time.monotonic() < deadline
                time.sleep(.01)
            size = 2 * 1024 ** 2
            response = client.get(address(relays, "rust", "file"),
                                  headers={"Range": f"bytes=0-{size - 1}"})
            assert response.status_code == 206 and len(response.content) == size
            assert response.content == b"".join(pattern(offset, 65536)
                                                for offset in range(0, size, 65536))
            health = client.get(relays["rust"].url + "/health").json()
            assert health["starting"] == 1 and health["startup_yields"] > 0
        finally:
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        deadline = time.monotonic() + 3
        while True:
            health = client.get(relays["rust"].url + "/health").json()
            if health["starting"] == health["active"] == 0:
                break
            assert time.monotonic() < deadline
            time.sleep(.01)


def test_successful_transfer_metrics_and_logs_do_not_expose_targets(relays):
    with httpx.Client(trust_env=False) as client:
        before = client.get(relays["rust"].url + "/health").json()
        response = client.get(address(relays, "rust", "file"), headers={"Range": "bytes=0-8191"})
        assert response.content == pattern(0, 8192)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            after = client.get(relays["rust"].url + "/health").json()
            if after["completed"] > before["completed"]:
                break
            time.sleep(.01)
        assert after["completed"] > before["completed"]
        assert after["bytes"] >= before["bytes"] + 8192
    log = relays["rust"].log.read_text(encoding="utf-8")
    assert all(value not in log for value in HEADERS.values())
    assert "/file" not in log


@pytest.mark.parametrize("mode", ["python", "rust"])
def test_independent_consumers_verify_every_object(relays, mode):
    from scripts.benchmark_rust_media import independent_load
    import hashlib

    result = independent_load(
        address(relays, mode, "file"), concurrency=4, duration=.1, size=8192,
        expected=hashlib.sha256(pattern(0, 8192)).hexdigest(),
        trace_prefix=f"contract-{mode}-{time.time_ns()}",
    )
    assert result["requests"] >= 4
    assert result["bytes"] == result["requests"] * 8192
    assert result["hashes_match"]


@pytest.mark.parametrize("mode", ["python", "rust"])
def test_new_seek_and_health_remain_available_during_large_transfer(relays, mode):
    entered, stop = threading.Event(), threading.Event()
    failures = []

    def playing():
        try:
            with httpx.Client(trust_env=False, timeout=3) as client:
                with client.stream("GET", address(relays, mode, "large")) as response:
                    assert response.status_code == 200
                    for chunk in response.iter_raw(65536):
                        assert chunk
                        entered.set()
                        if stop.wait(.002):
                            break
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=playing)
    thread.start()
    try:
        assert entered.wait(3)
        with httpx.Client(trust_env=False, timeout=2) as client:
            for index in range(12):
                offset = index * 11939
                response = client.get(address(relays, mode, "large"), headers={
                    "Range": f"bytes={offset}-{offset + 8191}",
                })
                assert response.status_code == 206 and response.content == pattern(offset, 8192)
                assert client.get(relays[mode].url + "/health").status_code == 200
    finally:
        stop.set()
        thread.join(4)
    assert not thread.is_alive() and not failures
