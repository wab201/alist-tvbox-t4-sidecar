import asyncio

import httpx
import pytest

from tests.rust.relay_support import binary_path, experiment, pattern


@pytest.mark.skipif(not binary_path().is_file(), reason="experimental relay not built")
def test_admission_bypass_head_and_release_do_not_deadlock():
    async def check(instances):
        async with asyncio.timeout(5), httpx.AsyncClient(trust_env=False, timeout=3) as client:
            base = instances["rust"].url
            headers = {"Range": "bytes=0-7", "x-t4-relay-client-trace": "admission-held"}
            async with client.stream("GET", base + "/t4/media/hedge-gated-body", headers=headers) as held:
                assert held.status_code == 206
                queued = asyncio.create_task(client.get(base + "/t4/media/file", headers={
                    "Range": "bytes=0-7", "x-t4-relay-client-trace": "admission-queued",
                }))
                try:
                    await asyncio.sleep(.03)
                    assert not queued.done()
                    direct = await client.get(base + "/t4/media/file", headers={
                        "Range": "bytes=0-7", "x-t4-relay-client-trace": "admission-bypass",
                        "x-t4-relay-admission-bypass": "1",
                    })
                    assert direct.content == pattern(0, 8)
                    assert (await client.head(base + "/t4/media/file")).status_code == 200
                    await client.post(instances["origin"].url + "/__release_body")
                    assert await held.aread() == pattern(0, 8)
                    assert (await queued).content == pattern(0, 8)
                finally:
                    queued.cancel()
                    await asyncio.gather(queued, return_exceptions=True)
    with experiment(trace=True, relay_args=("--startup-limit", "1")) as instances:
        asyncio.run(check(instances))
