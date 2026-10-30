import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.at.native import NativeAtClient, NativeAtInvoker
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.adapter import T4Adapter


@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_group_search_clicks_resolve_real_share_and_episode_after_memory_release(tmp_path, transport):
    requests = []

    def upstream(request):
        requests.append(request)
        query = request.url.params
        if request.url.path == "/play/native-token":
            assert query["id"] == "original-episode-id"
            return httpx.Response(200, json={
                "parse": 0, "url": "https://video.example/actual.mp4",
                "header": {"User-Agent": "original-player"},
            })
        assert request.url.path == "/pansou-group/native-token"
        if "wd" in query:
            return httpx.Response(200, json={"list": [{
                "vod_id": "pgroup:fixture:quark", "vod_name": "Quark",
                "vod_tag": "folder",
            }]})
        if "t" in query:
            assert query["t"] == "pgroup:fixture:quark"
            return httpx.Response(200, json={
                "page": 1, "pagecount": 1, "total": 1, "list": [{
                    "vod_id": "https://share.example/original", "vod_name": "Actual movie",
                    "vod_pic": "https://img.example/actual.jpg",
                }],
            })
        assert query["id"] == "https://share.example/original"
        return httpx.Response(200, json={"list": [{
            "vod_id": "https://share.example/original", "vod_name": "Actual movie",
            "vod_play_from": "Original", "vod_play_url": "Episode$original-episode-id",
        }]})

    async def subscription(_):
        return {"sites": [{
            "key": "csp_FishPanSouGroup", "name": "Grouped search",
            "api": "csp_FishPanSouGroup",
            "ext": {"api": "https://at.example", "token": "native-token"},
        }]}

    class NoWorker:
        def call(self, *_):
            raise AssertionError("grouped builtin must remain on AT HTTP")

    registry = SourceRegistry((SourceConfig(
        source_id="fixture", display_name="Fixture", at_base_url="https://at.example",
        subscription_id="1", subscription_token="subscription-token",
    ),))
    native = NativeAtClient(transport=httpx.MockTransport(upstream))
    adapter = T4Adapter(registry, NoWorker(), subscription_fetcher=subscription,
                        native_invoker=NativeAtInvoker(native))
    app = create_app(Settings(plugin_dir=str(tmp_path), log_file=None), registry,
                     subscription_fetcher=subscription, t4_adapter=adapter)
    try:
        with TestClient(app) as client:
            path = "/t4/fm/fixture/csp_FishPanSouGroup/"

            def call(params):
                if transport == "GET":
                    response = client.get(path, params=params)
                elif transport == "JSON":
                    response = client.post(path, json=params)
                else:
                    response = client.post(path, data=params)
                assert response.status_code == 200
                return response.json()

            result = call({"wd": "Actual movie", "quick": "false", "pg": "1"})
            assert len(requests) == 2
            card = result["list"][0]
            assert card["vod_id"].startswith("t4_")
            assert card.get("vod_tag") != "folder"
            assert card["vod_name"] == "Actual movie" and "Quark" in card["vod_remarks"]
            adapter.release_memory()
            detail = call({"ac": "detail", "ids": card["vod_id"]})["list"][0]
            episode = detail["vod_play_url"].split("$", 1)[1]
            assert episode.startswith("t4_")
            adapter.release_memory()
            play = call({"play": episode, "flag": "Original"})
            assert play["url"] == "https://video.example/actual.mp4"
            assert play["header"] == {"User-Agent": "original-player"}
            assert len(requests) == 4
    finally:
        adapter.close()
        native.close()
