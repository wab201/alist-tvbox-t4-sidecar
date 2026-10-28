import json

import httpx
import pytest

from sidecar.at.native import NativeAtClient, NativeAtUnsupportedError
from sidecar.at.subscription import PluginDescriptor


def builtin(api: str) -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id=api,
        name=api,
        plugin_type="builtin",
        api=api,
        source="",
        loader="",
        raw=False,
        token="native-token",
        secret="native-secret",
        local_proxy_config={},
        data=None,
        filters=(),
        backend_api="https://at.example",
    )


def test_native_bilibili_catalog_and_play_keep_original_routes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/bilibili/native-token"):
            if "ids" in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "list": [
                            {
                                "vod_id": "BV-native",
                                "vod_play_url": "正片$BV-native",
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={"class": [{"type_id": "follow:1"}], "list": []},
            )
        if request.url.path.endswith("/play/native-token"):
            return httpx.Response(
                200,
                json={
                    "parse": 0,
                    "url": "https://video.example/native.m3u8",
                    "header": {"Referer": "https://www.bilibili.com"},
                },
            )
        raise AssertionError(str(request.url))

    client = NativeAtClient(transport=httpx.MockTransport(handler))
    try:
        home = client.call(builtin("csp_BiliBili"), "home", {})
        detail = client.call(
            builtin("csp_BiliBili"),
            "detail",
            {"ids": ["BV-native"], "client": "com.fongmi.android.tv"},
        )
        play = client.call(
            builtin("csp_BiliBili"),
            "play",
            {"id": "BV-native", "dash": True},
        )
    finally:
        client.close()

    assert home["class"][0]["type_id"] == "follow:1"
    assert detail["list"][0]["vod_id"] == "BV-native"
    assert play["url"].endswith(".m3u8")
    assert requests[0].headers["X-CLIENT"] == "com.fongmi.android.tv"
    assert requests[1].url.params["ids"] == "BV-native"
    assert requests[2].url.params["bvid"] == "BV-native"
    assert requests[2].url.params["dash"] == "true"
    assert requests[2].headers["X-CLIENT"] == "t4-sidecar"


def test_native_media_detail_uses_id_and_preserves_action_play_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/media/native-token"):
            assert request.url.params["id"] == "msub:20"
            return httpx.Response(
                200,
                json={
                    "list": [
                        {
                            "vod_id": "msub:20",
                            "vod_play_from": "我的追剧$$$操作",
                            "vod_play_url": "01$msubep-20-1#检查更新$msubinspect-20",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/play/native-token"):
            return httpx.Response(
                200,
                json={"parse": 0, "url": "https://video.example/episode.m3u8"},
            )
        raise AssertionError(str(request.url))

    client = NativeAtClient(transport=httpx.MockTransport(handler))
    try:
        detail = client.call(
            builtin("csp_Media"),
            "detail",
            {"ids": ["msub:20"]},
        )
        play = client.call(
            builtin("csp_Media"),
            "action",
            {"id": "msubinspect-20"},
        )
    finally:
        client.close()

    assert detail["list"][0]["vod_id"] == "msub:20"
    assert "msubinspect-20" in detail["list"][0]["vod_play_url"]
    assert play["url"].endswith(".m3u8")
    assert seen[1].url.params["id"] == "msubinspect-20"
    assert seen[1].url.params["from"] == "jar"
    assert seen[1].url.params["type"] == "client-proxy"


def test_native_category_for_vod_preserves_filters_and_pagination() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"list": []})

    client = NativeAtClient(transport=httpx.MockTransport(handler))
    try:
        client.call(
            builtin("csp_AList"),
            "category",
            {
                "tid": "all",
                "pg": "2",
                "native_filter": {"type": "movie", "year": "2026"},
                "sort": "desc",
                "size": "30",
                "depth": "1",
            },
        )
    finally:
        client.close()

    params = seen[0].url.params
    assert params["t"] == "all"
    assert params["pg"] == "2"
    assert params["f"] == '{"type":"movie","year":"2026"}'
    assert params["sort"] == "desc"
    assert params["size"] == "30"
    assert params["depth"] == "1"
    assert params["type"] == "0"


@pytest.mark.parametrize("api", ["csp_Emby", "csp_BiliBili", "csp_TgWeb"])
def test_vod_ac_fix_does_not_strip_other_backends_detail_parameters(api):
    seen = []
    client = NativeAtClient(transport=httpx.MockTransport(
        lambda request: (seen.append(request) or httpx.Response(200, json={"list": []}))
    ))
    try:
        client.call(builtin(api), "detail", {
            "ids": ["fixture"], "ac": "quark", "depth": "2", "title": "Fixture",
        })
    finally:
        client.close()
    assert seen[0].url.params["ac"] == "quark"
    if api == "csp_TgWeb":
        assert seen[0].url.params["title"] == "Fixture"
    else:
        assert seen[0].url.params["depth"] == "2"


@pytest.mark.parametrize("method,params", [
    ("home", {}), ("category", {"tid": "all"}), ("search", {"key": "sample"}),
])
def test_alist_and_xiaoya_keep_distinct_vod_types(method, params):
    seen = []
    client = NativeAtClient(transport=httpx.MockTransport(
        lambda request: (seen.append(request) or httpx.Response(200, json={"list": []}))
    ))
    try:
        client.call(builtin("csp_AList"), method, params)
        client.call(builtin("csp_XiaoYa"), method, params)
    finally:
        client.close()
    assert seen[0].url.path == seen[1].url.path == "/vod/native-token"
    assert seen[0].url.params["type"] == "0"
    assert "type" not in seen[1].url.params


def test_native_pansou_uses_original_home_and_detail_parameter_names() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"list": []})

    client = NativeAtClient(transport=httpx.MockTransport(handler))
    try:
        client.call(builtin("csp_FishPanSou"), "home", {})
        client.call(
            builtin("csp_FishPanSou"),
            "detail",
            {"ids": ["pansou:1"]},
        )
    finally:
        client.close()

    assert seen[0].url.params["t"] == "0"
    assert seen[1].url.params["id"] == "pansou:1"


def test_native_emby_play_uses_the_original_emby_play_route() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"url": "https://video.example/emby.m3u8"})

    client = NativeAtClient(transport=httpx.MockTransport(handler))
    try:
        result = client.call(
            builtin("csp_Emby"),
            "play",
            {"id": "1@/Movies/demo.mkv", "t": "0"},
        )
    finally:
        client.close()

    assert result["url"].endswith(".m3u8")
    assert seen[0].url.path.endswith("/emby-play/native-token")
    assert seen[0].url.params["id"] == "1@/Movies/demo.mkv"
    assert seen[0].url.params["t"] == "0"


def test_native_telegram_web_and_channel_keep_distinct_original_routes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"class": [], "list": []})

    client = NativeAtClient(transport=httpx.MockTransport(handler))
    try:
        client.call(builtin("csp_TgWeb"), "home", {})
        client.call(
            builtin("csp_TgWeb"),
            "detail",
            {"ids": ["https://share.example/1"], "ac": "quark", "title": "Demo"},
        )
        client.call(
            builtin("csp_TgChannel"),
            "category",
            {"tid": "type:5", "pg": "2", "size": "20"},
        )
        client.call(
            builtin("csp_TgChannel"),
            "search",
            {"key": "ubuntu", "pg": "3", "size": "10"},
        )
    finally:
        client.close()

    assert seen[0].url.path.endswith("/tg-search/native-token")
    assert seen[0].url.params["web"] == "true"
    assert seen[1].url.path.endswith("/tg-search/native-token")
    assert seen[1].url.params["id"] == "https://share.example/1"
    assert seen[1].url.params["ac"] == "quark"
    assert seen[1].url.params["title"] == "Demo"
    assert seen[2].url.path.endswith("/tgsc/native-token")
    assert seen[2].url.params["t"] == "type:5"
    assert seen[2].url.params["pg"] == "2"
    assert seen[2].url.params["size"] == "20"
    assert seen[3].url.path.endswith("/tgsc/native-token")
    assert seen[3].url.params["wd"] == "ubuntu"
    assert seen[3].url.params["pg"] == "3"
    assert seen[3].url.params["size"] == "10"


def test_native_live_play_uses_the_original_live_play_route() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"url": "https://video.example/live.m3u8"})

    client = NativeAtClient(transport=httpx.MockTransport(handler))
    try:
        result = client.call(
            builtin("csp_Live"),
            "play",
            {"id": "live-1"},
        )
    finally:
        client.close()

    assert result["url"].endswith(".m3u8")
    assert seen[0].url.path.endswith("/live-play/native-token")
    assert seen[0].url.params["id"] == "live-1"


@pytest.mark.parametrize("url", [
    "https://live.example/stream.flv?sign=fixture",
    "https://at.example/p/native-token/0@123",
])
def test_live_detail_stream_is_delivered_without_calling_placeholder(url):
    def unexpected(_):
        raise AssertionError("a published live stream must not call /live-play")
    client = NativeAtClient(transport=httpx.MockTransport(unexpected))
    try:
        assert client.call(builtin("csp_Live"), "play", {"id": url}) == {
            "parse": 0, "jx": 0, "url": url, "header": {},
        }
    finally:
        client.close()


def test_live_detail_preserves_platform_parameter():
    seen = []
    client = NativeAtClient(transport=httpx.MockTransport(
        lambda request: (seen.append(request) or httpx.Response(200, json={"list": []}))
    ))
    try:
        client.call(builtin("csp_Live"), "detail", {"ids": ["bili$1"], "platform": "web"})
        assert seen[0].url.params["platform"] == "web"
    finally:
        client.close()


def test_native_client_rejects_client_local_builtin_without_server_route() -> None:
    client = NativeAtClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    try:
        try:
            client.call(builtin("csp_Push"), "home", {})
        except NativeAtUnsupportedError as exc:
            assert "server-side T4 route" in str(exc)
        else:
            raise AssertionError("expected unsupported builtin error")
    finally:
        client.close()
