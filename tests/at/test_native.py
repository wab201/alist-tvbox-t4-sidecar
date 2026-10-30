import json

import httpx
import pytest

from sidecar.at.native import NativeAtClient, NativeAtError, NativeAtUnsupportedError
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


@pytest.mark.parametrize("api,route", [
    ("csp_FishPanSou", "/pansou"),
    ("csp_FishPanSouGroup", "/pansou-group"),
])
def test_native_pansou_uses_original_home_and_detail_parameter_names(api, route) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"list": []})

    client = NativeAtClient(transport=httpx.MockTransport(handler))
    try:
        client.call(builtin(api), "home", {})
        client.call(builtin(api), "category", {"tid": "pgroup:fixture:quark", "pg": "2"})
        client.call(
            builtin(api),
            "detail",
            {"ids": ["pansou:1"]},
        )
    finally:
        client.close()

    assert seen[0].url.params["t"] == "0"
    assert seen[1].url.params["t"] == "pgroup:fixture:quark"
    assert seen[1].url.params["pg"] == "2"
    assert seen[2].url.params["id"] == "pansou:1"
    assert all(request.url.path == route + "/native-token" for request in seen)


def test_grouped_search_empty_home_is_not_an_invalid_json_error():
    client = NativeAtClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    try:
        assert client.call(builtin("csp_FishPanSouGroup"), "home", {}) == {"class": [], "list": []}
        with pytest.raises(NativeAtError, match="not JSON"):
            client.call(builtin("csp_FishPanSouGroup"), "search", {"key": "Movie"})
        with pytest.raises(NativeAtError, match="not JSON"):
            client.call(builtin("csp_FishPanSou"), "home", {})
    finally:
        client.close()


@pytest.mark.parametrize("official", [False, True])
def test_grouped_search_expands_cached_folders_without_prefetching_detail(official):
    from dataclasses import replace

    seen = []
    plugin = builtin("csp_FishPanSouGroup")
    if official:
        plugin = replace(plugin, t4_endpoint="https://at.example/t4/native-token/group")

    def handler(request):
        seen.append(request)
        query = request.url.params
        if "wd" in query:
            return httpx.Response(200, json={"list": [
                {"vod_id": "pgroup:fixture:quark", "vod_name": "Quark",
                 "vod_tag": "folder", "vod_remarks": "21 results"},
                {"vod_id": "pgroup:fixture:115", "vod_name": "115",
                 "vod_tag": "folder", "vod_remarks": "1 result"},
            ]})
        assert "ids" not in query and "id" not in query
        assert query["pg"] == "2"
        provider = query["t"].rsplit(":", 1)[-1]
        return httpx.Response(200, json={
            "page": 2, "pagecount": 2 if provider == "quark" else 1,
            "total": 21 if provider == "quark" else 1,
            "list": ([{"vod_id": "https://share.example/quark", "vod_name": "Real title",
                       "vod_pic": "https://img.example/actual.jpg", "vod_remarks": "HD",
                       "vod_play_from": "original-channel"}] if provider == "quark" else []),
        })

    client = NativeAtClient(transport=httpx.MockTransport(handler))
    try:
        result = client.call(plugin, "search", {"key": "Movie", "pg": "2", "quick": False})
    finally:
        client.close()
    assert len(seen) == 3
    assert result["page"] == 2 and result["pagecount"] == 2 and result["total"] == 22
    assert result["limit"] == 1
    card = result["list"][0]
    assert card["vod_id"] == "https://share.example/quark"
    assert card["vod_name"] == "Real title"
    assert card["vod_pic"] == "https://img.example/actual.jpg"
    assert card["vod_play_from"] == "original-channel"
    assert "Quark" in card["vod_remarks"] and "HD" in card["vod_remarks"]
    assert all(request.url.path == ("/t4/native-token/group" if official
                                   else "/pansou-group/native-token") for request in seen)


@pytest.mark.parametrize("all_fail", [False, True])
def test_grouped_search_reports_failed_groups_without_hiding_all_group_failure(all_fail):
    def handler(request):
        if "wd" in request.url.params:
            return httpx.Response(200, json={"list": [
                {"vod_id": "pgroup:fixture:quark", "vod_name": "Quark", "vod_tag": "folder"},
                {"vod_id": "pgroup:fixture:115", "vod_name": "115", "vod_tag": "folder"},
            ]})
        if all_fail or request.url.params["t"].endswith(":quark"):
            return httpx.Response(503, json={"message": "Unavailable"})
        return httpx.Response(200, json={"list": [
            {"vod_id": "https://share.example/115", "vod_name": "Actual"}],
            "pagecount": 1, "total": 1})

    client = NativeAtClient(transport=httpx.MockTransport(handler))
    try:
        if all_fail:
            with pytest.raises(NativeAtError):
                client.call(builtin("csp_FishPanSouGroup"), "search", {"key": "Movie"})
        else:
            result = client.call(builtin("csp_FishPanSouGroup"), "search", {"key": "Movie"})
            assert len(result["list"]) == 1
            assert result["t4_warnings"][0]["code"] == "group_expansion_failed"
            assert result["t4_warnings"][0]["upstream_status"] == 503
    finally:
        client.close()


def test_grouped_search_keeps_ordinary_shares_and_nonsearch_folder_navigation():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"list": [
            {"vod_id": "https://share.example/already-leaf", "vod_name": "Actual"},
        ]})

    client = NativeAtClient(transport=httpx.MockTransport(handler))
    try:
        result = client.call(builtin("csp_FishPanSouGroup"), "search", {"key": "Movie"})
        client.call(builtin("csp_FishPanSouGroup"), "category", {
            "tid": "pgroup:fixture:quark", "pg": "3",
        })
        client.call(builtin("csp_FishPanSou"), "search", {"key": "Movie"})
    finally:
        client.close()
    assert len(seen) == 3
    assert result == {"list": [{"vod_id": "https://share.example/already-leaf", "vod_name": "Actual"}]}
    assert seen[1].url.params["t"] == "pgroup:fixture:quark"
    assert seen[1].url.params["pg"] == "3"
    assert seen[2].url.path == "/pansou/native-token"


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


@pytest.mark.parametrize("api", ["csp_TgWeb", "csp_TgChannel", "csp_TgDouBan"])
def test_telegram_published_video_does_not_enter_cloud_play(api):
    def unexpected(_):
        raise AssertionError("Telegram /v/ media must not be parsed as a cloud ID")
    client = NativeAtClient(transport=httpx.MockTransport(unexpected))
    url = "https://media.example/v/fixture?sign=private"
    try:
        assert client.call(builtin(api), "play", {"id": url}) == {
            "parse": 0, "jx": 0, "url": url, "header": {},
        }
    finally:
        client.close()


@pytest.mark.parametrize("message,reason", [
    ("Invalid share link format https://private.example/secret", "invalid_share"),
    ("找不到迅雷云盘帐号 private-secret", "provider_account"),
    ("链接已失效 private-secret", "share_unavailable"),
    ("https://private.example/secret", "http_status"),
])
@pytest.mark.parametrize("field", ["message", "detail"])
def test_native_failure_classifies_without_exposing_response(message, reason, field):
    client = NativeAtClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(400, json={field: message})
    ))
    try:
        with pytest.raises(NativeAtError) as caught:
            client.call(builtin("csp_TgWeb"), "detail", {"ids": ["fixture"]})
        error = caught.value.public_error()
        assert error["upstream_status"] == 400 and error["reason"] == reason
        assert "private" not in json.dumps(error) and "private" not in str(caught.value)
    finally:
        client.close()
