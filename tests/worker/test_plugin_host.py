import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.plugin_proxy import rewrite_chunks
from sidecar.worker.host import PluginHost


SOURCE = """
from base.spider import Spider as Base
import io
class Spider(Base):
    def init(self, extend):
        self.setCache("key", "retained")
        self.setCache("deleted", "gone")
        self.delCache("deleted")
    def getName(self): return "Host fixture"
    def homeContent(self, filter):
        return {"list": [], "proxy": self.getProxyUrl(),
                "key": self.siteKey, "cache": self.getCache("key"),
                "deleted": self.getCache("deleted")}
    def localProxy(self, param):
        print("proxy-fixture-private-cookie")
        kind = param.get("type")
        if kind == "error": raise RuntimeError("private-signed-url")
        if kind == "headers":
            return [200, "application/json", __import__("json").dumps({
                "range": param.get("Range"), "headers": param["headers"],
                "request_header": __import__("json").loads(param["header"]),
            })]
        if kind == "manifest":
            return [200, "application/vnd.apple.mpegurl",
                    "#EXTM3U\\n#EXTINF:5,\\n" + self.getProxyUrl() + "&type=bytes\\n",
                    {"Connection": "keep-alive"}]
        if kind == "stream": return [200, "application/octet-stream", io.BytesIO(b"stream-bytes")]
        if kind == "range":
            return [206, "video/mp2t", b"345", {"Content-Range": "bytes 3-5/10"}]
        if kind == "redirect":
            return [302, "text/plain", "", {"Location": self.getProxyUrl() + "&type=bytes"}]
        return [200, "video/mp2t", b"0123456789", {}]
"""


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with TestClient(app, base_url="https://public.example:15443") as client:
        yield client


def uploaded(client, name="host.py"):
    response = client.post("/t4/api/plugins/upload", files={"file": (
        name, (SOURCE + "\n# " + name).encode(),
    )})
    assert response.status_code == 200, response.text
    plugin = response.json()["plugin"]
    home = client.get(plugin["fm_path"]).json()
    return plugin, home


def test_real_worker_host_proxy_key_cache_binary_headers_and_streaming(client):
    plugin, home = uploaded(client)
    url = home["proxy"]
    assert url.startswith("https://public.example:15443/t4/proxy/")
    assert "127.0.0.1" not in url
    assert home["key"] == "local-uploaded:" + plugin["id"]
    assert home["cache"] == "retained" and home["deleted"] is None
    assert client.get(url + "&type=bytes").content == b"0123456789"
    assert client.get(url + "&type=stream").content == b"stream-bytes"
    response = client.get(url + "&type=headers", headers={"X-Custom": "preserved"})
    assert {k.lower(): v for k, v in response.json()["headers"].items()}["x-custom"] == "preserved"
    assert {k.lower(): v for k, v in response.json()["request_header"].items()}["x-custom"] == "preserved"
    # A handler's print must never corrupt the Worker JSON protocol.
    assert client.get(plugin["fm_path"]).json()["cache"] == "retained"
    assert "proxy-fixture-private-cookie" not in json.dumps(
        client.get("/t4/api/diagnostics/events").json()
    )


@pytest.mark.parametrize("value,status,expected", [
    ("bytes=2-4", 206, b"234"), ("bytes=6-", 206, b"6789"),
    ("bytes=-3", 206, b"789"), ("bytes=50-", 416, b""),
    ("bytes=4-2", 416, b""), ("bytes=-0", 416, b""),
    ("bytes=0-1,4-5", 416, b""),
])
def test_buffered_proxy_range_and_head(client, value, status, expected):
    _, home = uploaded(client)
    response = client.get(home["proxy"], headers={"Range": value})
    assert response.status_code == status
    assert response.content == expected
    head = client.head(home["proxy"], headers={"Range": value})
    assert head.status_code == status and head.content == b""
    assert head.headers["content-length"] == str(len(expected))
    assert head.headers["content-range"] == response.headers["content-range"]


def test_plugin_handled_range_is_forwarded_without_second_slice(client):
    _, home = uploaded(client)
    response = client.get(home["proxy"] + "&type=range", headers={"Range": "bytes=3-5"})
    assert response.status_code == 206 and response.content == b"345"
    assert response.headers["content-range"] == "bytes 3-5/10"


def test_manifest_and_redirect_never_publish_internal_worker_address(client):
    _, home = uploaded(client)
    url = home["proxy"]
    manifest = client.get(url + "&type=manifest")
    assert manifest.status_code == 200
    assert url in manifest.text and "127.0.0.1" not in manifest.text
    assert "content-length" not in manifest.headers
    redirect = client.get(url + "&type=redirect", follow_redirects=False)
    assert redirect.status_code == 302 and redirect.headers["location"] == url + "&type=bytes"
    assert client.get(redirect.headers["location"]).content == b"0123456789"


def test_proxy_failures_are_redacted_and_do_not_kill_worker(client):
    plugin, home = uploaded(client)
    response = client.get(home["proxy"] + "&type=error")
    assert response.status_code == 502
    assert "private-signed-url" not in response.text
    assert client.get(plugin["fm_path"]).status_code == 200


def test_refresh_delete_expire_only_their_own_proxy_session(client):
    plugin, home = uploaded(client)
    second, second_home = uploaded(client, "second.py")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: client.get(home["proxy"]).content, range(8)))
    assert results == [b"0123456789"] * 8
    assert client.post(f"/t4/api/plugins/{plugin['id']}/refresh").status_code == 200
    assert client.get(home["proxy"]).status_code == 410
    assert client.get(second_home["proxy"]).status_code == 200
    new = client.get(plugin["fm_path"]).json()
    assert new["proxy"] != home["proxy"]
    assert client.delete(f"/t4/api/plugins/{plugin['id']}").status_code == 200
    assert client.get(new["proxy"]).status_code == 410
    assert client.delete(f"/t4/api/plugins/{second['id']}").status_code == 200


def test_host_stream_is_closed_and_server_stops():
    stream = io.BytesIO(b"binary" * 1000)
    host = PluginHost(lambda _: [200, "application/octet-stream", stream], "fixture")
    endpoint = host.endpoint
    client = httpx.Client(trust_env=False, timeout=2)
    try:
        response = client.get(endpoint)
        assert response.content == b"binary" * 1000
        assert stream.closed
    finally:
        host.close()
    try:
        assert not host._thread.is_alive()
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            client.get(endpoint)
    finally:
        client.close()


@pytest.mark.parametrize("size", [1, 2, 6, 7, 8, 30])
def test_rewrite_handles_urls_split_across_stream_chunks(size):
    raw = b"prefix http://worker/path middle http://worker/path suffix"
    async def run():
        async def chunks():
            for n in range(0, len(raw), size):
                yield raw[n:n + size]
        return b"".join([
            part async for part in rewrite_chunks(chunks(), b"http://worker/path", b"https://public/path")
        ])
    assert asyncio.run(run()) == (
        raw.replace(b"http://worker/path", b"https://public/path")
    )


def test_gate_cleanup_does_not_expire_interactive_proxy_and_loopback_ignores_env_proxy(client, monkeypatch):
    # The source fixture has no external HTTP dependency; loopback must remain local.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    plugin, home = uploaded(client)
    assert client.get(home["proxy"]).content == b"0123456789"
    assert client.post("/t4/api/compatibility/runs", json={}).status_code == 202
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        report = client.get("/t4/api/compatibility/latest").json()["report"]
        if report["state"] == "completed":
            break
        time.sleep(.05)
    assert report["state"] == "completed"
    assert report["gate"]["passed"] is False
    assert client.get(home["proxy"]).content == b"0123456789"
    assert client.get(plugin["fm_path"]).json()["proxy"] == home["proxy"]
