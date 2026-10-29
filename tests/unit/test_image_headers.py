import asyncio
import base64
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.at.native import NativeAtClient
from sidecar.at.subscription import SubscriptionParser
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.image_headers import match_image_headers, parse_image_header_rules
from sidecar.images import ImageBridge, split_image
from tests.unit.test_compatibility import PNG


def rules(cookie="private-cookie"):
    return parse_image_header_rules([
        {"host": ".example", "header": {"Referer": "https://owner.example/", "Cookie": cookie}},
        {"host": r"img[0-9]+\.example", "header": {"referer": "https://last.example/"}},
    ])


def test_header_matching_uses_containment_or_fullmatch_and_order():
    headers = rules()
    assert match_image_headers("img1.example", headers) == {
        "referer": "https://last.example/", "cookie": "private-cookie",
    }
    assert match_image_headers("prefix.img1.example", headers)["referer"] == "https://owner.example/"
    assert match_image_headers("other.test", headers) == {}
    assert match_image_headers("IMG1.EXAMPLE", headers) == {}
    assert "private-cookie" not in repr(headers)
    assert match_image_headers("any.test", parse_image_header_rules([
        {"host": "", "header": {"User-Agent": "all-hosts"}},
    ])) == {"user-agent": "all-hosts"}


def test_invalid_rules_do_not_disable_valid_headers_or_echo_values():
    headers = parse_image_header_rules([
        None, {}, {"host": "fixture", "header": "private"},
        {"host": ["fixture"], "header": {"Cookie": "private"}},
        {"host": "fixture", "header": {"Cookie": 42, "X-Bad": "private\r\nother"}},
        {"host": "fixture", "header": {"Bad Header": "private", "Referer": " https://ok/ "}},
        {"host": "[", "header": {"X-Literal": "works"}},
    ])
    assert len(headers) == 2
    assert match_image_headers("fixture", headers) == {"referer": "https://ok/"}
    assert match_image_headers("[", headers) == {"x-literal": "works"}
    assert parse_image_header_rules({}) == ()


def test_subscription_rules_are_server_only_and_shared_across_descriptors():
    snapshot = SubscriptionParser("https://at.example").parse("source", {
        "headers": [{"host": "image.test", "header": {"Cookie": "private-cookie"}}],
        "sites": [{"key": "a", "api": "csp_Media"}, {"key": "b", "api": "csp_BiliBili"}],
    })
    a, b = snapshot.plugins
    assert a.image_headers is b.image_headers
    assert match_image_headers("image.test", a.image_headers) == {"cookie": "private-cookie"}
    assert "private-cookie" not in repr(a)
    assert "private-cookie" not in json.dumps(snapshot.public_metadata())
    assert "private-cookie" not in base64.b64decode(a.worker_extend()).decode()


def test_fongmi_inline_headers_object_is_not_sent_as_headers_header():
    assert split_image(
        'https://i.test/p.png@Headers={"Cookie":"a=x=y","User-Agent":"Fixture"}@Referer=https://owner/'
    ) == ("https://i.test/p.png", {
        "Cookie": "a=x=y", "User-Agent": "Fixture", "Referer": "https://owner/",
    })
    with pytest.raises(ValueError):
        split_image("https://i.test/p.png@Headers=[]")


def test_subscription_overrides_inline_and_unmatched_images_stay_direct():
    async def run():
        seen = []
        images = ImageBridge(transport=httpx.MockTransport(
            lambda request: (seen.append(request) or httpx.Response(200, content=PNG))
        ))
        try:
            url = "https://other.test/p.jpg@320w.webp"
            assert images.prepare(url, "http://gateway", "http://at", rules=rules()) == url
            path = images.prepare(
                'https://img1.example/p.jpg@{"Referer":"https://inline/","Cookie":"inline"}',
                "http://gateway", "http://at", rules=rules(),
            )
            await images.fetch(path.rsplit("/", 1)[-1])
            assert seen[0].headers["referer"] == "https://last.example/"
            assert seen[0].headers["cookie"] == "private-cookie"
        finally:
            await images.aclose()
    asyncio.run(run())


def test_same_image_with_different_subscription_cookies_has_isolated_cache():
    async def run():
        seen = []
        images = ImageBridge(transport=httpx.MockTransport(
            lambda request: (seen.append(request.headers["cookie"]) or httpx.Response(200, content=PNG))
        ))
        try:
            paths = [
                images.prepare("https://img1.example/p.jpg", "http://gateway", "http://at", rules=rules(value))
                for value in ("first-private", "second-private")
            ]
            assert paths[0] != paths[1]
            for path in paths:
                await images.fetch(path.rsplit("/", 1)[-1])
            assert seen == ["first-private", "second-private"]
        finally:
            await images.aclose()
    asyncio.run(run())


def test_redirect_rechecks_rules_and_never_uses_another_subscription_cookie_jar():
    async def run():
        seen = []

        def upstream(request):
            seen.append(request)
            if len(seen) == 1:
                assert request.headers["cookie"] == "private-first"
                return httpx.Response(302, headers={
                    "Location": "https://second.test/p.jpg", "Set-Cookie": "ambient=private; Path=/",
                })
            if len(seen) == 2:
                assert request.headers["cookie"] == "private-second"
                return httpx.Response(302, headers={"Location": "https://third.test/p.jpg"})
            assert "cookie" not in request.headers and "authorization" not in request.headers
            return httpx.Response(200, content=PNG)

        images = ImageBridge(transport=httpx.MockTransport(upstream))
        try:
            images.client.cookies.set("previous", "private-other", domain="third.test")
            first = parse_image_header_rules([
                {"host": "first.test", "header": {"Cookie": "private-first"}},
                {"host": "second.test", "header": {"Cookie": "private-second"}},
            ])
            second = parse_image_header_rules([
                {"host": "first.test", "header": {"Cookie": "private-first"}},
                {"host": "second.test", "header": {"Cookie": "different"}},
            ])
            value = 'https://first.test/p.jpg@{"Authorization":"private-inline"}'
            path = images.prepare(value, "http://gateway", "http://at", rules=first)
            assert images.prepare(value, "http://gateway", "http://at", rules=second) != path
            assert await images.fetch(path.rsplit("/", 1)[-1]) == (PNG, "image/png")
            assert len(seen) == 3
        finally:
            await images.aclose()
    asyncio.run(run())


def test_redirect_loop_is_bounded_and_responses_are_closed():
    async def run():
        streams = []

        class Stream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b""

            async def aclose(self):
                self.closed = True

        def upstream(_):
            stream = Stream()
            streams.append(stream)
            return httpx.Response(302, headers={"Location": "/loop"}, stream=stream)

        images = ImageBridge(transport=httpx.MockTransport(upstream))
        try:
            path = images.prepare("https://img1.example/loop", "http://gateway", "http://at", rules=rules())
            with pytest.raises(httpx.TooManyRedirects):
                await images.fetch(path.rsplit("/", 1)[-1])
            assert len(streams) == 21 and all(stream.closed for stream in streams)
            assert images._bytes == 0
        finally:
            await images.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("prefix", ["", "/prefix"])
def test_real_gateway_uses_refreshed_subscription_rules_without_client_credentials(tmp_path, prefix):
    version = 1
    seen = []

    async def subscription(_):
        return {
            "headers": [{"host": "image.test", "header": {"Cookie": f"private-{version}"}}],
            "sites": [{"key": "fixture", "api": "csp_Media", "ext": {"token": "private-token"}}],
        }

    def upstream(request):
        seen.append(request.headers["cookie"])
        return httpx.Response(200, content=PNG)

    app = create_app(
        Settings(plugin_dir=str(tmp_path)),
        SourceRegistry((SourceConfig("at", "AT", subscription_url="https://at.example/sub/private/1"),)),
        subscription_fetcher=subscription,
    )
    with patch.object(NativeAtClient, "_request", return_value={"list": [{
        "vod_id": "fixture", "vod_name": "Fixture", "vod_pic": "https://image.test/p.png",
    }]}):
        asyncio.run(app.state.images.client.aclose())
        app.state.images.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        with TestClient(app, base_url="https://public.test" + prefix, root_path=prefix) as client:
            api = "/t4/fm/at/fixture/"
            paths = []
            for params in ({}, {"t": "1"}, {"wd": "test"}, {"ac": "detail", "ids": "fixture"}):
                result = client.get(api, params=params)
                assert result.status_code == 200
                path = result.json()["list"][0]["vod_pic"]
                paths.append(path)
                assert path.startswith("https://public.test" + prefix + "/t4/images/")
                assert client.get(path).content == PNG
                assert client.head(path).headers["content-type"] == "image/png"
            assert len(set(paths)) == 1 and seen == ["private-1"]
            version = 2
            assert client.get("/t4/api/sources/at/subscription?refresh=true").status_code == 200
            path = client.get(api).json()["list"][0]["vod_pic"]
            assert path != paths[0] and client.get(path).content == PNG
            assert seen == ["private-1", "private-2"]
            for endpoint in (
                "/t4/config.json", "/t4/api/sources/at/subscription", "/t4/api/diagnostics/export",
            ):
                text = client.get(endpoint).text
                assert "private-1" not in text and "private-2" not in text and "image.test" not in text
