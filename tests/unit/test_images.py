import asyncio
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.compatibility.probes import ResourceProbes
from sidecar.at.native import NativeAtClient
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.images import ImageBridge, split_image
from sidecar.playback import PlaybackBridge
from tests.unit.test_compatibility import PNG


@pytest.mark.parametrize("url", [
    "https://i.example/p.jpg@320w_200h_1c.webp",
    "https://i.example/p.jpg?email=a@example.com",
])
def test_resize_and_url_at_are_not_image_headers(url):
    assert split_image(url) == (url, {})


def test_explicit_image_headers_preserve_resize_and_equals_in_values():
    url = "https://i.example/p.jpg@320w_200h.webp"
    assert split_image(url + '@{"Cookie":"a=x=y","Referer":"https://a.example/@user"}') == (
        url, {"Cookie": "a=x=y", "Referer": "https://a.example/@user"},
    )
    assert split_image(url + "@Referer=https://owner.example/@User-Agent=Fixture") == (
        url, {"Referer": "https://owner.example/", "User-Agent": "Fixture"},
    )


def test_image_probe_treats_cdn_resize_as_literal_url():
    seen = []
    bridge = PlaybackBridge()
    probe = ResourceProbes(bridge, httpx.MockTransport(
        lambda request: (seen.append(str(request.url)) or httpx.Response(200, content=PNG))
    ))
    try:
        url = "https://i.example/p.jpg@320w_200h_1c.webp"
        assert probe.image(url)["status"] == "pass"
        assert seen == [url]
    finally:
        probe.close()
        bridge.close()


def test_lazy_cover_proxy_normalizes_mime_and_bounds_cache():
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.headers["referer"] == "https://movie.douban.com/"
        return httpx.Response(200, content=PNG, headers={"Content-Type": "text/html"})

    async def run():
        images = ImageBridge(transport=httpx.MockTransport(upstream), cache_bytes=len(PNG), capacity=2)
        try:
            paths = [images.prepare(
                f"https://img1.doubanio.com/p/{i}.jpg", "https://public.example", "https://at.example",
            ) for i in range(3)]
            assert seen == []
            with pytest.raises(KeyError):
                await images.fetch(paths[0].rsplit("/", 1)[-1])
            for path in paths[1:]:
                assert await images.fetch(path.rsplit("/", 1)[-1]) == (PNG, "image/png")
                assert images._bytes <= len(PNG)
            await images.fetch(paths[2].rsplit("/", 1)[-1])
            assert len(seen) == 2
        finally:
            await images.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("body", [b"<html>error</html>", PNG * 4])
def test_image_proxy_rejects_error_pages_and_oversized_images(body):
    async def run():
        images = ImageBridge(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
            max_bytes=len(PNG) * 2,
        )
        try:
            path = images.prepare("https://img1.doubanio.com/p.jpg", "http://gateway", "http://at")
            with pytest.raises(ValueError):
                await images.fetch(path.rsplit("/", 1)[-1])
            assert images._bytes == 0
        finally:
            await images.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("prefix", ["", "/prefix"])
def test_list_detail_image_delivery_through_actual_gateway(tmp_path, prefix):
    async def subscription(_):
        return {"sites": [{"key": "tg", "api": "csp_TgWeb", "ext": {"token": "private-token"}}]}

    def upstream(request):
        assert request.headers["referer"] == "https://movie.douban.com/"
        return httpx.Response(200, content=PNG, headers={"Content-Type": "text/html"})

    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry((SourceConfig(
        "at", "AT", subscription_url="https://at.example/sub/private/1",
    ),)), subscription_fetcher=subscription)
    with patch.object(NativeAtClient, "_request", return_value={"list": [{
        "vod_id": "fixture", "vod_name": "Fixture",
        "vod_pic": "https://img1.doubanio.com/p.jpg?sign=private-sign",
    }]}):
        asyncio.run(app.state.images.client.aclose())
        app.state.images.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        with TestClient(app, base_url="https://public.example" + prefix, root_path=prefix) as client:
            api = "/t4/fm/at/tg/"
            card = client.get(api).json()["list"][0]
            assert card["vod_pic"].startswith("https://public.example" + prefix + "/t4/images/")
            assert "private" not in card["vod_pic"]
            image = client.get(card["vod_pic"])
            assert image.content == PNG and image.headers["content-type"] == "image/png"
            head = client.head(card["vod_pic"])
            assert head.content == b"" and head.headers["content-length"] == str(len(PNG))
            detail = client.get(api, params={"ac": "detail", "ids": card["vod_id"]}).json()["list"][0]
            assert detail["vod_pic"] == card["vod_pic"]
            logs = client.get("/t4/api/diagnostics/export").text
            assert "private-sign" not in logs and "doubanio" not in logs
            events = client.get("/t4/api/diagnostics/events").json()["events"]
            assert any(
                event["stage"] == "request.complete" and event.get("route") == "image"
                and event.get("status") == 200 for event in events
            )
            assert client.get("/t4/images/expired").status_code == 410


def test_image_cache_coalesces_requests_and_expires_without_new_lists():
    calls = 0

    async def upstream(_):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, content=PNG)

    async def run():
        images = ImageBridge(transport=httpx.MockTransport(upstream), ttl=30)
        try:
            path = images.prepare("/images?url=fixture", "https://gateway/prefix", "https://at.example")
            assert path.startswith("https://gateway/prefix/t4/images/")
            key = path.rsplit("/", 1)[-1]
            results = await asyncio.gather(*(images.fetch(key) for _ in range(12)))
            assert results == [(PNG, "image/png")] * 12
            assert calls == 1 and images._bytes == len(PNG)
            images._assets[key].touched -= 31
            with pytest.raises(KeyError):
                await images.fetch(key)
            assert images._bytes == 0 and not images._assets
        finally:
            await images.aclose()
    asyncio.run(run())


def test_ordinary_cover_is_direct_and_explicit_headers_override_defaults():
    async def run():
        seen = []
        images = ImageBridge(transport=httpx.MockTransport(
            lambda request: (seen.append(request) or httpx.Response(200, content=PNG))
        ))
        try:
            direct = "https://ordinary.example/p.jpg@320w.webp"
            assert images.prepare(direct, "https://gateway", "https://at.example") == direct
            path = images.prepare(
                'https://img1.doubanio.com/p.jpg@{"Referer":"https://owner.example/"}',
                "https://gateway", "https://at.example",
            )
            await images.fetch(path.rsplit("/", 1)[-1])
            assert seen[0].headers["referer"] == "https://owner.example/"
        finally:
            await images.aclose()
    asyncio.run(run())


def test_cover_download_concurrency_is_bounded_and_eviction_during_io_is_accounted():
    active, peak = 0, 0

    async def upstream(_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, content=PNG)

    async def run():
        images = ImageBridge(transport=httpx.MockTransport(upstream), capacity=16, cache_bytes=len(PNG) * 2)
        try:
            paths = [images.prepare(
                f"https://img1.doubanio.com/{index}.png", "http://gateway", "http://at",
            ) for index in range(12)]
            downloads = [asyncio.create_task(images.fetch(path.rsplit("/", 1)[-1])) for path in paths]
            await asyncio.sleep(0.005)
            for index in range(20, 40):
                images.prepare(f"https://img1.doubanio.com/{index}.png", "http://gateway", "http://at")
            assert await asyncio.gather(*downloads) == [(PNG, "image/png")] * 12
            assert 1 < peak <= 4
            assert images._bytes == sum(len(asset.content or b"") for asset in images._assets.values()) == 0
            assert len(images._assets) <= 16
        finally:
            await images.aclose()
    asyncio.run(run())


def test_at_cover_route_and_public_reverse_proxy_prefix_are_both_preserved():
    async def run():
        seen = []
        images = ImageBridge(transport=httpx.MockTransport(
            lambda request: (seen.append(request) or httpx.Response(200, content=PNG))
        ))
        try:
            path = images.prepare(
                "/images?url=fixture", "https://public.example/tv/", "https://at.example/at",
            )
            assert path.startswith("https://public.example/tv/t4/images/")
            assert images.prepare(
                "https://at.example/at/images?url=fixture", "https://public.example/tv/", "https://at.example/at",
            ) == path
            await images.fetch(path.rsplit("/", 1)[-1])
            assert str(seen[0].url) == "https://at.example/at/images?url=fixture"
        finally:
            await images.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("statuses,calls,passes", [
    ([500, 200], 2, True),
    ([502, 502], 2, False),
    ([403], 1, False),
    ([404], 1, False),
])
def test_only_transient_server_errors_get_one_cover_retry(statuses, calls, passes):
    seen = []

    def upstream(request):
        status = statuses[len(seen)]
        seen.append(request)
        return httpx.Response(status, content=PNG if status == 200 else b"unavailable")

    async def run():
        images = ImageBridge(transport=httpx.MockTransport(upstream))
        try:
            key = images.prepare("/images?url=fixture", "http://gateway", "https://at.example").rsplit("/", 1)[-1]
            if passes:
                assert await images.fetch(key) == (PNG, "image/png")
            else:
                with pytest.raises(httpx.HTTPStatusError):
                    await images.fetch(key)
            assert len(seen) == calls and images._bytes == (len(PNG) if passes else 0)
        finally:
            await images.aclose()
    asyncio.run(run())
