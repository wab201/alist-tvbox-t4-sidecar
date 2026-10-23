import asyncio

from fastapi.testclient import TestClient

from sidecar.at.subscription import PluginDescriptor
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.adapter import T4Adapter, T4Request


class FakeWorker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def call(self, runtime_key, plugin, method, params):
        self.calls.append((runtime_key, method, params))
        if method == "home":
            return {
                "class": [{"type_id": "all", "type_name": "All"}],
                "list": [{"vod_id": "movie-1", "vod_name": "Demo"}],
            }
        if method == "detail":
            return {
                "list": [
                    {
                        "vod_id": params["ids"][0],
                        "vod_name": "Demo detail",
                        "vod_play_from": "Demo",
                        "vod_play_url": "正片$raw-play-id",
                    }
                ]
            }
        if method == "play":
            return {
                "parse": 0,
                "url": "https://video.invalid/demo.m3u8",
                "header": {"User-Agent": "test"},
            }
        return {"list": []}


class FakeNative:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def call(self, runtime_key, plugin, method, params):
        self.calls.append((runtime_key, method, params))
        if method == "home":
            return {
                "class": [{"type_id": "follow:1"}],
                "list": [{"vod_id": "native-home"}],
            }
        if method == "detail":
            return {
                "list": [
                    {
                        "vod_id": params["ids"][0],
                        "vod_play_from": "BiliBili",
                        "vod_play_url": "正片$native-play",
                    }
                ]
            }
        if method == "play":
            return {"parse": 0, "url": "https://video.invalid/native.m3u8"}
        return {"list": []}


def descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="demo",
        name="Demo",
        plugin_type="raw_python",
        api="csp_PyProxy",
        source="https://at.example/plugins/demo.py",
        loader="https://at.example/Atvp/token/v1.py",
        raw=True,
        token="private-token",
        secret="private-secret",
        local_proxy_config={},
        data=None,
        filters=(),
    )


def source() -> SourceConfig:
    return SourceConfig(
        source_id="source-a",
        display_name="Source A",
        at_base_url="https://at.example",
        subscription_id="1",
        subscription_token="sub-token",
    )


async def fetcher(_source):
    return {
        "sites": [
            {
                "key": "demo",
                "name": "Demo",
                "api": "csp_PyProxy",
                "ext": {
                    "source": "https://at.example/plugins/demo.py",
                    "loader": "https://at.example/Atvp/token/v1.py",
                    "raw": True,
                },
            }
        ]
    }


def test_t4_adapter_wraps_detail_and_play_ids() -> None:
    worker = FakeWorker()
    adapter = T4Adapter(
        SourceRegistry((source(),)),
        worker,
        subscription_fetcher=fetcher,
    )

    home = asyncio.run(
        adapter.dispatch(T4Request("home", "source-a", None, {}))
    )
    opaque_detail_id = home["list"][0]["vod_id"]
    assert opaque_detail_id.startswith("t4_")
    assert "private-token" not in opaque_detail_id

    detail = asyncio.run(
        adapter.dispatch(
            T4Request(
                "detail",
                "source-a",
                None,
                {"ids": opaque_detail_id},
            )
        )
    )
    opaque_play_id = detail["list"][0]["vod_play_url"].split("$", 1)[1]
    assert opaque_play_id.startswith("t4_")

    play = asyncio.run(
        adapter.dispatch(
            T4Request(
                "play",
                "source-a",
                None,
                {"id": opaque_play_id, "flag": "Demo"},
            )
        )
    )
    assert play["url"] == "https://video.invalid/demo.m3u8"
    assert worker.calls[1][2]["ids"] == ["movie-1"]
    assert worker.calls[2][2]["id"] == "raw-play-id"


def test_t4_route_supports_get_and_post() -> None:
    worker = FakeWorker()
    adapter = T4Adapter(
        SourceRegistry((source(),)),
        worker,
        subscription_fetcher=fetcher,
    )
    client = TestClient(
        create_app(
            Settings(gateway_token="gateway-token"),
            SourceRegistry((source(),)),
            subscription_fetcher=fetcher,
            t4_adapter=adapter,
        )
    )

    get_response = client.get(
        "/t4/api/home",
        params={"source": "source-a"},
        headers={"X-T4-Token": "gateway-token"},
    )
    post_response = client.post(
        "/t4/api/home",
        json={"source": "source-a"},
        headers={"X-T4-Token": "gateway-token"},
    )
    query_response = client.get(
        "/t4/api/home",
        params={"source": "source-a", "t4_token": "gateway-token"},
    )

    assert get_response.status_code == 200
    assert post_response.status_code == 200
    assert query_response.status_code == 200
    assert get_response.json()["list"][0]["vod_id"].startswith("t4_")
    assert post_response.json()["list"][0]["vod_id"].startswith("t4_")


def test_t4_route_is_open_in_local_mode() -> None:
    worker = FakeWorker()
    adapter = T4Adapter(
        SourceRegistry((source(),)),
        worker,
        subscription_fetcher=fetcher,
    )
    client = TestClient(
        create_app(
            Settings(),
            SourceRegistry((source(),)),
            subscription_fetcher=fetcher,
            t4_adapter=adapter,
        )
    )

    response = client.get(
        "/t4/api/home",
        params={"source": "source-a"},
    )

    assert response.status_code == 200


async def builtin_fetcher(_source):
    return {
        "sites": [
            {
                "key": "csp_BiliBili",
                "name": "BiliBili",
                "api": "csp_BiliBili",
                "ext": {
                    "api": "https://at.example",
                    "token": "native-token",
                },
            }
        ]
    }


def test_t4_adapter_routes_builtin_to_native_invoker() -> None:
    native = FakeNative()
    adapter = T4Adapter(
        SourceRegistry((source(),)),
        FakeWorker(),
        subscription_fetcher=builtin_fetcher,
        native_invoker=native,
    )

    home = asyncio.run(adapter.dispatch(T4Request("home", "source-a", None, {})))
    opaque_detail_id = home["list"][0]["vod_id"]
    detail = asyncio.run(
        adapter.dispatch(
            T4Request("detail", "source-a", "csp_BiliBili", {"ids": opaque_detail_id})
        )
    )
    opaque_play_id = detail["list"][0]["vod_play_url"].split("$", 1)[1]
    play = asyncio.run(
        adapter.dispatch(
            T4Request("play", "source-a", "csp_BiliBili", {"play": opaque_play_id})
        )
    )

    assert play["url"].endswith(".m3u8")
    assert native.calls[0][1] == "home"
    assert native.calls[1][2]["ids"] == ["native-home"]
    assert native.calls[2][2]["id"] == "native-play"


def test_t4_adapter_forwards_client_and_telegram_detail_context() -> None:
    native = FakeNative()
    adapter = T4Adapter(
        SourceRegistry((source(),)),
        FakeWorker(),
        subscription_fetcher=lambda _source: builtin_fetcher(_source),
        native_invoker=native,
    )

    asyncio.run(
        adapter.dispatch(
            T4Request(
                "detail",
                "source-a",
                "csp_BiliBili",
                {
                    "ids": "recommend",
                    "ac": "detail",
                    "title": "Demo",
                    "client": "com.fongmi.android.tv",
                },
            )
        )
    )

    assert native.calls[0][2]["client"] == "com.fongmi.android.tv"
    assert native.calls[0][2]["ids"] == ["recommend"]
