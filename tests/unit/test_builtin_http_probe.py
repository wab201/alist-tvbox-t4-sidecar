import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

from scripts.verify_builtin_playback import BuiltinHttpAdapter
from sidecar.t4.adapter import T4Request
from sidecar.worker.client import WorkerRemoteError


@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_builtin_probe_uses_fm_contract_for_detail_and_play(transport):
    seen = []

    def backend(request):
        assert request.url.path == "/t4/fm/env-at/csp_XiaoYa/"
        if transport == "GET":
            assert request.method == "GET"
            params = dict(request.url.params)
        elif transport == "JSON":
            assert request.method == "POST"
            params = json.loads(request.content)
        else:
            assert request.method == "POST"
            params = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        seen.append(params)
        return httpx.Response(200, json={"ok": True})

    async def run():
        async with httpx.AsyncClient(
            base_url="https://gateway.example", transport=httpx.MockTransport(backend),
        ) as http:
            adapter = BuiltinHttpAdapter(http, transport)
            detail = T4Request("detail", "env-at", "csp_XiaoYa", {"ids": "fixture"})
            assert await adapter.dispatch(detail) == {"ok": True}
            assert detail.params == {"ids": "fixture"}
            await adapter.dispatch(T4Request(
                "play", "env-at", "csp_XiaoYa", {"play": "t4_fixture", "flag": "Original"},
            ))

    asyncio.run(run())
    assert seen == [
        {"ids": "fixture", "ac": "detail"},
        {"play": "t4_fixture", "flag": "Original"},
    ]


def test_builtin_probe_preserves_error_code_without_upstream_details():
    async def run():
        async with httpx.AsyncClient(
            base_url="https://gateway.example",
            transport=httpx.MockTransport(lambda _: httpx.Response(502, json={
                "detail": {"code": "native_failed", "message": "private upstream details"},
            })),
        ) as http:
            with pytest.raises(WorkerRemoteError) as error:
                await BuiltinHttpAdapter(http, "GET").dispatch(T4Request(
                    "detail", "env-at", "csp_XiaoYa", {"ids": "fixture"},
                ))
            assert error.value.code == "native_failed"
            assert "private upstream details" not in str(error.value)

    asyncio.run(run())
