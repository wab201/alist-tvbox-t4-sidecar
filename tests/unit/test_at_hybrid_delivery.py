import asyncio
from contextlib import closing

import httpx
import pytest

from sidecar.at.native import NativeAtClient
from sidecar.playback import PlaybackBridge
from sidecar.plugin_proxy import PluginProxyRegistry
from tests.unit.test_cloud_playback import plugin
from tests.unit.test_direct_media_delivery import CountingStream


@pytest.mark.parametrize("server", [False, True])
@pytest.mark.parametrize("play_id", ["1@99", "msubep-20-1"])
def test_native_cloud_identity_matches_official_t4_policy(server, play_id):
    query = NativeAtClient._play_query(plugin(), {
        "id": play_id, "_server_proxy": server, "type": "client-proxy",
    })
    assert query["id"] == play_id
    assert query["type"] == ("t4" if server or play_id.startswith("msubep-") else "t4-direct")


@pytest.mark.parametrize("server", [False, True])
@pytest.mark.parametrize("kind", ["builtin", "official_t4"])
def test_official_t4_stays_at_owned_without_fake_policy_parameters(server, kind):
    seen = []
    target = plugin(plugin_type=kind, t4_endpoint="https://at.example/t4/fixture-token/v/cloud")
    native = NativeAtClient(transport=httpx.MockTransport(
        lambda request: (seen.append(request) or httpx.Response(
            200, json={"url": "https://cdn.example/video.mkv"},
        )),
    ))
    try:
        native.call(target, "play", {"id": "1@99", "_server_proxy": server})
        assert dict(seen[0].url.params) == {"play": "1@99", "flag": ""}
        assert seen[0].url.path == "/t4/fixture-token/v/cloud"
        assert target.public_metadata()["playback_policy_owner"] == "at"
    finally:
        native.close()


@pytest.mark.parametrize("headers", [{}, {"Cookie": "fixture-cookie"}])
@pytest.mark.parametrize("url", [
    "http://192.168.10.2/video.mkv", "http://10.1.2.3/video",
    "http://[fd00::1]/media", "http://at:4567/media", "http://localhost:5000/media",
])
def test_private_http_is_automatically_bridged_without_player_credentials(url, headers):
    bridge = PlaybackBridge()
    original = {"url": url, "parse": 0, "header": headers, "position": 42, "subs": []}
    try:
        result = bridge.prepare(original, "https://public.example/prefix")
        assert result["url"].startswith("https://public.example/prefix/t4/media/")
        assert result["header"] == {} and result["position"] == 42
        asset = bridge.get(result["url"].rsplit("/", 1)[-1])
        assert asset.url == url and asset.headers == headers
        assert asset.local_transport is True
        assert bridge.prepare(result, "https://public.example/prefix") == result
        assert bridge._size == 1
        assert original["url"] == url and original["header"] == headers
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_private_file_uses_internal_transport_instead_of_environment_proxy(
    monkeypatch, async_mode, method,
):
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.url.host == "at.internal"
        assert request.headers["range"] == "bytes=4-15"
        return httpx.Response(
            206, headers={"Content-Range": "bytes 4-15/100"},
            content=b"" if request.method == "HEAD" else b"fixture-data",
        )

    def unexpected_proxy(*args, **kwargs):
        raise AssertionError("private media must not use the environment-proxy client")

    bridge = PlaybackBridge(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(bridge.client, "send", unexpected_proxy)
    monkeypatch.setattr(bridge.media_client, "send", unexpected_proxy)
    try:
        result = bridge.prepare(
            {"url": "http://at.internal:4567/file.mkv"},
            "https://public.example",
        )
        asset = bridge.get(result["url"].rsplit("/", 1)[-1])
        status, _, body = request_media(
            bridge, asset, method, async_mode=async_mode,
        )
        assert status == 206 and body == (b"" if method == "HEAD" else b"fixture-data")
        assert len(seen) == 1 and seen[0].method == method
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()


@pytest.mark.parametrize("headers,proxied", [
    ({}, False), ({"User-Agent": "signed-agent"}, False),
    ({"Referer": "https://115.com/", "User-Agent": "signed-agent"}, False),
    ({"cOoKiE": "fixture-cookie"}, True), ({"AUTHORIZATION": "fixture-auth"}, True),
])
def test_public_http_preserves_direct_signature_headers_but_keeps_credentials_on_server(headers, proxied):
    bridge = PlaybackBridge()
    original = {"url": "https://cdn.example/file.mkv?sign=exact%2Fvalue", "header": headers}
    try:
        result = bridge.prepare(original, "https://public.example")
        assert ("/t4/media/" in result["url"]) is proxied
        if proxied:
            asset = bridge.get(result["url"].rsplit("/", 1)[-1])
            assert asset.headers == headers and asset.url == original["url"]
            assert result["header"] == {}
        else:
            assert result == original and not bridge._assets
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()


def test_existing_worker_bridge_is_not_wrapped_in_a_second_media_relay():
    proxies = PluginProxyRegistry()
    key = proxies.register("http://127.0.0.1:5000/proxy", "fixture", "fixture")
    bridge = PlaybackBridge(proxy_registry=proxies)
    try:
        payload = {"url": "https://public.example/t4/proxy/" + key + "?do=media", "header": {}}
        assert bridge.prepare(payload, "https://public.example") == payload
        assert bridge._size == 0
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()


def request_media(bridge, asset, method, *, async_mode, redirect=True):
    headers = {"range": "bytes=4-15", "if-range": '"fixture"', "user-agent": "real-player"}

    async def run():
        response = await bridge.open_media_async(asset, method, headers, redirect=redirect)
        try:
            return response.status_code, dict(response.headers), await response.aread()
        finally:
            await response.aclose()

    if async_mode:
        return asyncio.run(run())
    with closing(bridge.open_media(asset, method, headers, redirect=redirect)) as response:
        return response.status_code, dict(response.headers), response.read()


def at_asset(bridge, *, headers=None, play_id="1@99.mkv"):
    result = bridge.relay_at(
        {"url": "https://at.example/p/fixture-token/" + play_id, "header": headers or {}},
        "https://public.example", backend_api="https://at.example",
        token="fixture-token", internal_url="http://at.internal:4567",
    )
    assert bridge.prepare(result, "https://public.example") == result
    return bridge.get(result["url"].rsplit("/", 1)[-1])


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("intermediate", [False, True])
def test_at_public_redirect_is_delivered_without_connecting_to_cdn_or_reading_body(
    async_mode, method, intermediate,
):
    seen, reads, closed = [], [], []
    target = "https://cdn.example/file.mkv?sign=exact%2Fvalue&part=1%2B2"

    def upstream(request):
        seen.append(request)
        assert request.url.host == "at.internal"  # Connecting to the CDN is a regression.
        location = "/internal-hop" if intermediate and request.url.path.startswith("/p/") else target
        return httpx.Response(
            307, headers={"Location": location},
            stream=CountingStream(b"unread-redirect-body", reads, closed),
        )

    bridge = PlaybackBridge(transport=httpx.MockTransport(upstream))
    try:
        status, headers, body = request_media(
            bridge, at_asset(bridge), method, async_mode=async_mode,
        )
        assert status == 302 and headers["location"] == target and body == b""
        assert headers["cache-control"] == "no-store" and headers["content-length"] == "0"
        assert not reads and len(closed) == len(seen) == (2 if intermediate else 1)
        assert all(r.method == method for r in seen)
        assert all(r.headers["range"] == "bytes=4-15" for r in seen)
        assert all(r.headers["if-range"] == '"fixture"' for r in seen)
        assert all(r.headers["user-agent"] == "real-player" for r in seen)
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("boundary", [
    "explicit_ua", "explicit_referer", "explicit_cookie", "explicit_auth",
    "set_cookie", "session_cookie", "private_cdn", "logical_episode", "probe",
])
def test_at_redirect_retains_server_delivery_when_direct_handoff_is_not_valid(async_mode, boundary):
    seen, reads, closed = [], [], []
    target = "http://10.2.3.4/file.mkv" if boundary == "private_cdn" else "https://cdn.example/file.mkv"
    extra = {
        "explicit_ua": {"User-Agent": "signed-agent"},
        "explicit_referer": {"Referer": "https://required.example/"},
        "explicit_cookie": {"Cookie": "fixture-cookie"},
        "explicit_auth": {"Authorization": "fixture-auth"},
    }.get(boundary, {})
    data = b"fixture-media"

    def upstream(request):
        seen.append(request)
        if request.url.host == "at.internal":
            return httpx.Response(302, headers={
                "Location": target,
                **({"Set-Cookie": "session=fixture; Path=/"} if boundary == "set_cookie" else {}),
            })
        return httpx.Response(206, headers={
            "Content-Range": "bytes 4-16/100", "Content-Length": str(len(data)),
        }, stream=CountingStream(data if request.method != "HEAD" else b"", reads, closed))

    bridge = PlaybackBridge(transport=httpx.MockTransport(upstream))
    if boundary == "session_cookie":
        bridge._local_client.cookies.set("session", "fixture", domain="at.internal")
        bridge._local_media_client.cookies.set("session", "fixture", domain="at.internal")
    try:
        asset = at_asset(
            bridge, headers=extra,
            play_id="msubep-20-1.mkv" if boundary == "logical_episode" else "1@99.mkv",
        )
        status, headers, body = request_media(
            bridge, asset, "GET", async_mode=async_mode, redirect=boundary != "probe",
        )
        assert status == 206 and body == data and "location" not in headers
        assert len(seen) == 2 and reads == [len(data)] and closed
        assert seen[-1].headers["range"] == "bytes=4-15"
        assert seen[-1].headers["if-range"] == '"fixture"'
        if boundary == "explicit_ua":
            assert all(r.headers["user-agent"] == "signed-agent" for r in seen)
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()


@pytest.mark.parametrize("async_mode", [False, True])
def test_at_redirect_limit_closes_all_responses_without_prefetch(async_mode):
    closed = []
    bridge = PlaybackBridge(transport=httpx.MockTransport(lambda request: httpx.Response(
        302, headers={"Location": "/again"},
        stream=CountingStream(b"", [], closed),
    )))
    bridge._local_client.max_redirects = bridge._local_media_client.max_redirects = 2
    try:
        with pytest.raises(httpx.TooManyRedirects):
            request_media(bridge, at_asset(bridge), "GET", async_mode=async_mode)
        assert len(closed) == 3
    finally:
        asyncio.run(bridge.aclose())
        bridge.close()
