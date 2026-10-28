import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.at.native import NativeAtClient, NativeAtInvoker
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.adapter import T4Adapter


@pytest.mark.parametrize("api", ["csp_XiaoYa", "csp_AList"])
@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
@pytest.mark.parametrize("module_path", [False, True])
@pytest.mark.parametrize("with_ac", [False, True])
def test_vod_detail_to_play_consumes_client_ac_and_preserves_media_headers(
    tmp_path, api, transport, module_path, with_ac,
):
    seen = []
    media_url = "https://media.example/episode.mkv?signature=fixture"
    headers = {"Referer": "https://drive.example/", "User-Agent": "Fixture player"}
    raw_play_id = "12@345"

    async def subscription(_source):
        return {"sites": [{
            "key": api, "api": api,
            "ext": {"api": "https://at.example", "token": "fixture-token"},
        }]}

    def backend(request):
        seen.append(request)
        if request.url.path == "/vod/fixture-token":
            query = dict(request.url.params)
            assert query["ids"] == "movie-1"
            assert query["depth"] == "2"
            # AT changes the episode representation when this control leaks.
            play_id = (
                "https://at.example/p/fixture-token/" + raw_play_id
                if query.get("ac") == "detail" else raw_play_id
            )
            return httpx.Response(200, json={"list": [{
                "vod_id": "movie-1", "vod_name": "Fixture", "vod_play_from": "Original",
                "vod_play_url": "Episode 1$" + play_id,
            }]})
        assert request.url.path == "/play/fixture-token"
        if request.url.params["id"] != raw_play_id:
            return httpx.Response(400, json={"message": "invalid site parameter"})
        assert request.url.params["from"] == "jar"
        assert request.url.params["type"] == "client-proxy"
        return httpx.Response(200, json={
            "parse": 0, "jx": 0, "url": media_url, "header": headers,
        })

    registry = SourceRegistry((SourceConfig(
        "at", "AT", subscription_url="https://at.example/sub/fixture/1",
    ),))
    native = NativeAtClient(transport=httpx.MockTransport(backend))
    adapter = T4Adapter(
        registry, object(), subscription_fetcher=subscription,
        native_invoker=NativeAtInvoker(native),
    )
    app = create_app(
        Settings(plugin_dir=str(tmp_path)), registry,
        subscription_fetcher=subscription, t4_adapter=adapter,
    )
    try:
        with TestClient(app) as client:
            def call(module, params):
                path = f"/t4/fm/at/{api}/" + (f"api/{module}" if module_path else "")
                if transport == "GET":
                    return client.get(path, params=params)
                if transport == "JSON":
                    return client.post(path, json=params)
                return client.post(path, data=params)

            detail = call("detail", {
                "ids": "movie-1", "depth": "2",
                **({"ac": "detail"} if with_ac else {}),
            })
            assert detail.status_code == 200
            vod = detail.json()["list"][0]
            play_id = vod["vod_play_url"].partition("$")[2]
            assert play_id.startswith("t4_")
            play = call("play", {"play": play_id, "flag": "Original"})
            assert play.status_code == 200
            assert play.json() == {
                "parse": 0, "jx": 0, "url": media_url, "header": headers,
            }
            assert "ac" not in seen[0].url.params
            assert len(seen) == 2
    finally:
        native.close()
