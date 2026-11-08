import asyncio
import hashlib

import httpx
import pytest

from scripts.check_subscription_samples import (
    ObservedInvoker, SamplingAdapter, audit_gate, prefix_read, rotated_payload, safe_mime, throttled,
)
from sidecar.t4.adapter import T4Request

MEDIA = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00"


def test_rounds_select_different_published_cards_without_changing_input():
    payload = {"list": [{"vod_id": str(i)} for i in range(4)]}
    for round_index in range(3):
        result = rotated_payload(payload, "category", round_index)
        assert result["list"][0]["vod_id"] == str(round_index)
    assert payload["list"][0]["vod_id"] == "0"


def test_rounds_select_other_real_episodes_with_flags_preserved():
    payload = {"list": [{
        "vod_play_from": "a$$$b",
        "vod_play_url": "one$id-1#two$id-2$$$one$id-3#two$id-4",
    }]}
    result = rotated_payload(payload, "detail", 1)
    assert result["list"][0]["vod_play_url"] == "two$id-2#one$id-1$$$two$id-4#one$id-3"
    assert result["list"][0]["vod_play_from"] == "a$$$b"
    assert payload["list"][0]["vod_play_url"].startswith("one$id-1")


@pytest.mark.parametrize("status", [200, 206])
def test_prefix_probe_reads_and_hashes_finite_window(status):
    async def run():
        headers = {"Content-Range": "bytes 0-15/100"} if status == 206 else {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, headers=headers, stream=httpx.ByteStream(MEDIA)),
        )) as client:
            return await prefix_read(client, "https://cdn.example", {}, limit=16)
    result = asyncio.run(run())
    assert result["passed"]
    assert result["bytes"] == 16
    assert result["sha256"] == hashlib.sha256(MEDIA).hexdigest()
    assert result["range_supported"] == (status == 206)


def test_200_without_range_is_not_claimed_as_seek_support():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(MEDIA * 1000)),
        )) as client:
            return await prefix_read(client, "https://cdn.example", {}, limit=16)
    result = asyncio.run(run())
    assert result["passed"] and result["bytes"] == 16 and not result["range_supported"]


@pytest.mark.parametrize("complete_range", [False, True])
def test_complete_range_consumes_eof_and_preserves_connection(complete_range):
    async def run():
        peers, tasks = [], set()

        async def serve(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                while await reader.readuntil(b"\r\n\r\n"):
                    peers.append(writer.get_extra_info("peername")[1])
                    writer.write(b"HTTP/1.1 206 Partial Content\r\nContent-Length: 16\r\n"
                                 b"Content-Range: bytes 0-15/100\r\n\r\n" + MEDIA)
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        async with server:
            async with httpx.AsyncClient(trust_env=False) as client:
                url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/media"
                for _ in range(3):
                    result = await prefix_read(client, url, {}, limit=16, complete_range=complete_range)
                    assert result["passed"] and result["response_exhausted"] == complete_range
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert len(peers) == 3 and len(set(peers)) == (1 if complete_range else 3)
    asyncio.run(run())


@pytest.mark.parametrize("case", ["ok", "overflow", "short", "ignore-range"])
def test_complete_range_rejects_overflow_short_and_unbounded_200(case):
    async def run():
        data = MEDIA + b"extra" if case == "overflow" else MEDIA[:-1] if case == "short" else MEDIA
        status = 200 if case == "ignore-range" else 206
        headers = {"Content-Range": "bytes 0-15/100"} if status == 206 else {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, headers=headers, stream=httpx.ByteStream(data)),
        )) as client:
            return await prefix_read(client, "https://fixture.example", {}, limit=16, complete_range=True)
    result = asyncio.run(run())
    assert result["passed"] == (case == "ok")
    if case == "ok":
        assert result["response_exhausted"]


@pytest.mark.parametrize("range_header", ["bytes 1-4/100", "bytes 0-2/100", "bytes 0-4/4"])
def test_broken_range_is_not_accepted(range_header):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(206, headers={"Content-Range": range_header},
                                          stream=httpx.ByteStream(b"data")),
        )) as client:
            return await prefix_read(client, "https://cdn.example", {}, limit=4)
    assert not asyncio.run(run())["passed"]


def test_errors_export_only_exception_type():
    async def run():
        def handle(request):
            raise httpx.ReadError("https://private.example?secret=secret")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await prefix_read(client, "https://private.example?secret=secret", {})
    result = asyncio.run(run())
    assert result["error"] == "ReadError" and "secret" not in str(result)


def test_throttle_does_not_read_exception_text():
    error = RuntimeError("private-token")
    assert not throttled(error)
    error.details = {"http_status": 429}
    assert throttled(error)


def test_sampling_records_stable_underlying_identifier_without_exporting_it():
    class Adapter:
        def unwrap_id(self, value, **kwargs):
            return "private-real-id"
        async def dispatch(self, request):
            return {"list": []}
    adapter = SamplingAdapter(Adapter(), 0, 0, lambda: None)
    asyncio.run(adapter.dispatch(T4Request("detail", "source", "plugin", {"ids": "opaque-id"})))
    assert len(adapter.operations) == 1
    assert adapter.operations[0]["identity_sha256"] == hashlib.sha256(b"private-real-id").hexdigest()
    assert "private-real-id" not in str(adapter.operations)
    assert "dispatch_ms_without_pacing" in adapter.operations[0]


def test_html_error_page_cannot_count_as_media():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"Content-Type": "text/html"},
                                          stream=httpx.ByteStream(b"<html>upstream failed</html>")),
        )) as client:
            return await prefix_read(client, "https://cdn.example", {})
    result = asyncio.run(run())
    assert not result["passed"] and result["reason"] == "not_byte_media"


def test_backend_observer_preserves_return_and_only_captures_shape():
    class Invoker:
        def call(self, *args):
            return {"url": "https://private.example?token=secret", "list": [], "parse": 0}
    class Plugin:
        plugin_type = "secspider"
    observations = []
    value = ObservedInvoker(Invoker(), observations).call("runtime", Plugin(), "play", {})
    assert value["url"].endswith("secret")
    assert observations[0]["returned_type"] == "dict"
    assert observations[0]["url_scheme"] == "https"
    assert "secret" not in str(observations) and "private.example" not in str(observations)


def test_failed_dispatch_restores_original_invoker_and_keeps_failure_evidence():
    class Invoker:
        pass
    class Adapter:
        _worker = Invoker()
        async def dispatch(self, request):
            assert isinstance(self._worker, ObservedInvoker)
            raise ValueError("private-token")
    original = Adapter()
    invoker = original._worker
    adapter = SamplingAdapter(original, 0, 0, lambda: None)
    with pytest.raises(ValueError):
        asyncio.run(adapter.dispatch(T4Request("home", "source", "plugin", {})))
    assert original._worker is invoker
    assert adapter.operations[0]["error"] == "ValueError"
    assert "private-token" not in str(adapter.operations)


@pytest.mark.parametrize("body,headers", [
    (b"data", {}),
    (MEDIA, {"Content-Range": "bytes 0-15/100"}),
    (MEDIA, {"Content-Length": "100"}),
])
def test_short_unknown_and_inconsistent_200_are_not_accepted(body, headers):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, stream=httpx.ByteStream(body)),
        )) as client:
            return await prefix_read(client, "https://cdn.example", {}, limit=32)
    assert not asyncio.run(run())["passed"]


def test_gate_never_promotes_a_completed_audit_or_partial_round_to_compatibility_pass():
    batch = {"state": "completed", "gate": {"passed": False}, "plugins": []}
    report = {"rounds": [batch]}
    assert audit_gate(report, 1)["audit_completed"]
    assert not audit_gate(report, 1)["passed"]
    assert not audit_gate(report, 2)["audit_completed"]
    batch["gate"]["passed"] = True
    batch["plugins"] = [{"byte_relay": {"status": "fail"}}]
    assert not audit_gate(report, 1)["passed"]


def test_mime_report_cannot_contain_a_url_or_token():
    assert safe_mime("https://private.example?token=secret") == "unknown"
    assert safe_mime("video/mp4; charset=private-secret") == "video/mp4"


def test_aes_ciphertext_is_only_compared_as_bytes_after_independent_hls_validation():
    async def run(verified):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(206, headers={"Content-Range": "bytes 0-15/100"},
                                          stream=httpx.ByteStream(bytes(range(16)))),
        )) as client:
            return await prefix_read(client, "https://cdn.example", {}, limit=16,
                                     opaque_verified=verified)
    assert not asyncio.run(run(False))["passed"]
    verified = asyncio.run(run(True))
    assert verified["passed"] and verified["opaque_verified_by_hls_probe"]
    assert verified["format"] == ""
