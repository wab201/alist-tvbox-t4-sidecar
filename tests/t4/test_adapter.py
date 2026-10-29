import asyncio
from dataclasses import replace
import threading

import pytest
from fastapi.testclient import TestClient

from sidecar.at.subscription import PluginDescriptor, SubscriptionSnapshot
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.adapter import AtvpWorkerInvoker, T4Adapter, T4Request


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


def test_adapter_does_not_invent_play_ids_for_empty_episode_entries() -> None:
    class EmptyEpisodeWorker(FakeWorker):
        def call(self, runtime_key, plugin, method, params):
            return {"list": [{
                "vod_id": "movie",
                "vod_play_from": "Demo",
                "vod_play_url": "$#Placeholder$#Play$actual",
            }]}

    adapter = T4Adapter(
        SourceRegistry((source(),)), EmptyEpisodeWorker(), subscription_fetcher=fetcher
    )
    detail = asyncio.run(
        adapter.dispatch(T4Request("detail", "source-a", "demo", {"ids": "movie"}))
    )
    episodes = detail["list"][0]["vod_play_url"].split("#")
    assert episodes[:2] == ["$", "Placeholder$"]
    assert episodes[2].startswith("Play$t4_")


def test_detail_metadata_uses_only_the_exact_catalog_card_and_preserves_results():
    adapter = T4Adapter(SourceRegistry((source(),)), FakeWorker(), subscription_fetcher=fetcher)
    plugin = descriptor()
    card = {"vod_id": "movie-1", "vod_name": "Catalog title", "vod_pic": "https://image.example/cover"}
    adapter._normalize("category", {"list": [card]}, source(), plugin)
    raw = {"vod_id": "movie-1", "vod_name": "", "vod_play_url": "Episode$actual"}
    detail = adapter._normalize("detail", {"list": [raw]}, source(), plugin)["list"][0]
    assert detail["vod_name"] == card["vod_name"] and detail["vod_pic"] == card["vod_pic"]
    assert raw == {"vod_id": "movie-1", "vod_name": "", "vod_play_url": "Episode$actual"}
    assert adapter._normalize("detail", {"list": []}, source(), plugin) == {"list": []}
    for other_source, other_plugin in (
        (replace(source(), source_id="other"), plugin),
        (source(), replace(plugin, plugin_id="other")),
    ):
        other = adapter._normalize("detail", {"list": [raw]}, other_source, other_plugin)["list"][0]
        assert not other.get("vod_name") and not other.get("vod_pic")
    fresh = adapter._normalize("detail", {"list": [{**raw, "vod_name": "Fresh title"}]}, source(), plugin)
    assert fresh["list"][0]["vod_name"] == "Fresh title"
    asyncio.run(adapter.reset_plugin(source().source_id, plugin.plugin_id))
    assert not adapter._catalog_metadata


@pytest.mark.parametrize("field", ["pic", "poster", "cover", "thumb", "image"])
def test_actual_cover_alias_is_used_without_overriding_canonical_cover(field):
    adapter = T4Adapter(SourceRegistry((source(),)), FakeWorker())
    try:
        card = {"vod_id": "fixture", field: "https://image.example/real.jpg"}
        item = adapter._normalize_vod(card, source(), descriptor(), module="category")
        assert item["vod_pic"] == card[field] and "vod_pic" not in card
        card["vod_pic"] = "https://image.example/canonical.jpg"
        assert adapter._normalize_vod(card, source(), descriptor())["vod_pic"] == card["vod_pic"]
    finally:
        adapter.close()


def test_folder_category_requests_unwrap_opaque_card_ids(tmp_path) -> None:
    class FolderWorker(FakeWorker):
        def call(self, runtime_key, plugin, method, params):
            self.calls.append((runtime_key, method, params))
            if method == "home":
                return {"list": [{"vod_id": "atvp_detail:private-folder", "vod_name": "Folder",
                                  "vod_tag": "folder"}]}
            return {"list": []}

    registry = SourceRegistry((source(),))
    worker = FolderWorker()
    adapter = T4Adapter(registry, worker, subscription_fetcher=fetcher)
    app = create_app(Settings(plugin_dir=str(tmp_path)), registry, fetcher, adapter)
    with TestClient(app) as client:
        api = "/t4/fm/source-a/demo/"
        folder = client.get(api).json()["list"][0]["vod_id"]
        assert folder.startswith("t4_")
        for method in ("GET", "POST"):
            response = client.request(method, api, params={"t": folder, "pg": "2"})
            assert response.status_code == 200
            assert worker.calls[-1][2]["tid"] == "atvp_detail:private-folder"
        assert client.get(api, params={"t": "t4_expired"}).status_code == 400


def test_batch_close_reinitializes_navigation_workers_on_the_next_run() -> None:
    class Pool:
        def __init__(self):
            self.calls = []

        def request(self, key, method, params):
            self.calls.append(method)
            return {}

        def close(self):
            pass

    pool = Pool()
    invoker = AtvpWorkerInvoker(pool)
    invoker.call("source:child", descriptor(), "home", {})
    invoker.close()
    invoker.call("source:child", descriptor(), "home", {})
    assert pool.calls == ["init", "home", "init", "home"]


def test_t4_route_supports_get_and_post() -> None:
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

    get_response = client.get(
        "/t4/api/home",
        params={"source": "source-a"},
    )
    post_response = client.post(
        "/t4/api/home",
        json={"source": "source-a"},
    )

    assert get_response.status_code == 200
    assert post_response.status_code == 200
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


def test_adapter_runs_blocking_worker_calls_off_event_loop() -> None:
    barrier = threading.Barrier(2)

    class BarrierWorker:
        def call(self, runtime_key, plugin, method, params):
            if method == "home":
                barrier.wait(timeout=2)
            return {"list": []}

    snapshot = SubscriptionSnapshot(
        source_id="source-a",
        site_count=1,
        plugins=(descriptor(),),
    )
    adapter = T4Adapter(
        SourceRegistry((source(),)),
        BarrierWorker(),
        local_snapshot_provider=lambda _source_id: snapshot,
    )

    async def run() -> None:
        await asyncio.wait_for(
            asyncio.gather(
                adapter.dispatch(T4Request("home", "source-a", "demo", {})),
                adapter.dispatch(T4Request("home", "source-a", "demo", {})),
            ),
            timeout=3,
        )

    asyncio.run(run())


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
