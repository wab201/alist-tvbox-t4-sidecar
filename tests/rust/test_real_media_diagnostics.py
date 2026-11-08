import asyncio
import hashlib
import time
from types import SimpleNamespace

import httpx
import pytest

from scripts.benchmark_rust_real_media import classify, comparisons, paired_stages, range_metadata, read
from scripts.diagnose_rust_real_media import diagnose, internal_asset, measured_upstream, safe_failure, transport_summary
from tests.rust.relay_support import PROFILE_HEADER, binary_path, experiment


def response(status=206, **changes):
    headers = {"content-range": "bytes 4-7/100", "content-length": "4",
               "content-type": "video/mp4"}
    headers.update(changes)
    return httpx.Response(status, headers=headers)


def test_exact_range_contract():
    assert range_metadata(response(), 4, 4)["range_valid"]
    assert not range_metadata(response(200), 4, 4)["range_valid"]
    assert not range_metadata(response(**{"content-range": "bytes 5-8/100"}), 4, 4)["range_valid"]
    assert not range_metadata(response(**{"content-length": "100"}), 4, 4)["range_valid"]
    assert not range_metadata(response(**{"content-encoding": "gzip"}), 4, 4)["range_valid"]


def test_url_classification_does_not_retain_sensitive_url():
    result = classify("https://private.example/p/secret/file.mkv?signature=secret")
    assert result == {"scheme": "https", "at_proxy": True, "sidecar_proxy": False, "hls": False}
    assert "secret" not in str(result)
    assert classify("https://cdn.example/a.m3u8")["hls"]


def test_finite_read_verifies_full_hash():
    async def run():
        def handle(request):
            assert request.headers["range"] == "bytes=4-7"
            return httpx.Response(206, headers=response().headers, stream=httpx.ByteStream(b"data"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await read(client, "https://cdn.example/file", {}, 4, 4,
                              expected=hashlib.sha256(b"data").hexdigest())
    result = asyncio.run(run())
    assert result["complete"] and result["hash_match"] and result["bytes"] == 4


def test_errors_cannot_export_signed_urls():
    async def run():
        def handle(request):
            raise httpx.ConnectError("https://secret.example?token=secret", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await read(client, "https://secret.example?token=secret", {}, 0, 4)
    result = asyncio.run(run())
    assert result["error"] == "ConnectError"
    assert "secret" not in str(result)


def test_invalid_range_is_not_consumed():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(b"unexpected"))
        )) as client:
            return await read(client, "https://cdn.example/file", {}, 4, 4)
    result = asyncio.run(run())
    assert result["bytes"] == 0 and result["error"] == "range_contract"


def test_stages_are_paired_by_same_request():
    rows = paired_stages([{
        "trace": "one", "client_start_us": 1000, "client_first_body_us": 11000,
    }], [{
        "client_trace": "one", "handler_us": 2000, "upstream_request_us": 3000,
        "upstream_headers_us": 7000, "first_body_us": 9000,
    }, {"client_trace": "other", "upstream_headers_us": 1000000}])
    assert len(rows) == 1
    assert rows[0]["stages_ms"]["upstream_headers_wait"] == 4
    assert rows[0]["stages_ms"]["downstream_first_body"] == 2


def test_incomplete_or_failed_group_is_not_a_performance_comparison():
    rows = [
        {"mode": "python", "concurrency": 4, "passed": True},
        {"mode": "rust", "concurrency": 4, "passed": False},
    ]
    assert comparisons([{"lane": "at-proxy", "rows": rows}], repeats=1) == []


def test_catalog_failure_exports_only_fixed_markers():
    result = safe_failure(httpx.Response(400, json={
        "message": "找不到文件 /private/secret?token=secret", "code": 40001,
        "private": "secret",
    }))
    assert result["markers"] == ["file_missing"]
    assert result["json_code"] == 40001
    assert "secret" not in str(result)


def test_transport_events_are_paired_per_exchange():
    result = transport_summary([
        {"status": 302, "transport_us": {
            "connection.connect_tcp.started": 1000,
            "connection.connect_tcp.complete": 3000,
        }},
        {"status": 206, "transport_us": {
            "http11.receive_response_headers.started": 7000,
            "http11.receive_response_headers.complete": 12000,
        }},
    ])
    assert result[0]["stages_ms"] == {"connect_tcp": 2}
    assert result[1]["stages_ms"] == {"receive_headers": 5}
    assert not result[1]["new_connection_observed"]


def test_internal_asset_only_rewrites_configured_subscription_origin():
    asset = {"url": "https://at.example/p/token/1@2.mkv?sign=secret", "headers": {}}
    result = internal_asset(asset, "https://at.example/sub/token/0", "http://at:4567")
    assert result["url"] == "http://at:4567/p/token/1@2.mkv?sign=secret"
    assert internal_asset(asset, "https://other.example/sub/0", "http://at:4567") is None
    assert internal_asset({**asset, "url": "https://at.example/unrelated"},
                          "https://at.example/sub/0", "http://at:4567") is None
    assert internal_asset(asset, "https://at.example/sub/0", "http://user:secret@at") is None


def test_low_frequency_origin_probe_does_not_export_url():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(206, headers={
                "Content-Range": "bytes 0-3/100", "Content-Length": "4",
            }, stream=httpx.ByteStream(b"data")),
        )) as client:
            return await measured_upstream(client, {
                "url": "https://secret.example/private?token=secret", "headers": {},
            }, 4)
    result = asyncio.run(run())
    assert result["complete"] and result["bytes"] == 4
    assert "secret" not in str(result)


@pytest.mark.parametrize("failure", ["throttle", "wrong_bytes"])
def test_real_diagnostics_stops_before_further_requests(monkeypatch, failure):
    calls = []
    monkeypatch.delenv("ALIST_TVBOX_INTERNAL_URL", raising=False)
    async def measure(client, asset, size):
        calls.append(True)
        if failure == "throttle":
            return {"status": 429, "bytes": 0}
        return {"status": 206, "complete": True, "sha256": str(len(calls)), "bytes": size}
    async def sleep(interval):
        pass
    monkeypatch.setattr("scripts.diagnose_rust_real_media.measured_upstream", measure)
    monkeypatch.setattr("scripts.diagnose_rust_real_media.asyncio.sleep", sleep)
    report = {"passed": False, "lanes": []}
    asyncio.run(diagnose({"https-direct": {"url": "https://unused.example", "headers": {}}},
                         SimpleNamespace(origin_only=True, lanes=None, repeats=3, interval=1),
                         report))
    assert not report["passed"]
    assert report["stopped_for_throttle" if failure == "throttle" else "stopped_for_media_mismatch"]
    assert len(calls) == (1 if failure == "throttle" else 2)


@pytest.mark.skipif(not binary_path().is_file(), reason="Rust diagnostic binary unavailable")
def test_redirect_keeps_first_request_start_and_all_exchanges():
    with experiment(profile=True) as instances:
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.get(instances["python"].url + "/t4/media/redirect",
                                  headers={"Range": "bytes=0-3", PROFILE_HEADER: "redirect-test"})
            assert response.status_code == 206 and len(response.content) == 4
            deadline = time.monotonic() + 2
            event = None
            while event is None and time.monotonic() < deadline:
                events = client.get(instances["python"].url + "/__profile").json()["events"]
                event = next((v for v in events if v["trace"] == "redirect-test"), None)
                if event is None:
                    time.sleep(.01)
            assert event is not None
            first, second = event["upstream_exchanges"]
            assert event["upstream_request_us"] == first["started_us"]
            assert first["status"] == 302 and second["status"] == 206
            assert first["started_us"] <= first["headers_us"] <= second["started_us"]
            assert second["headers_us"] == event["upstream_headers_us"]
