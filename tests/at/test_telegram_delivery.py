import json
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from sidecar.at.native import NativeAtClient
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry


async def subscription(_):
    return {"sites": [{
        "key": "tg", "api": "csp_TgWeb", "ext": {"token": "private-token"},
    }]}


def app_for(tmp_path):
    return create_app(
        Settings(plugin_dir=str(tmp_path)),
        SourceRegistry((SourceConfig(
            "at", "AT", subscription_url="https://at.example/sub/private/1",
        ),)), subscription_fetcher=subscription,
    )


@pytest.mark.parametrize("method", ["GET", "JSON", "FORM"])
@pytest.mark.parametrize("route", ["fm", "api"])
def test_telegram_unlabelled_media_survives_full_t4_handoff(tmp_path, method, route):
    url = "https://media.example/v/fixture?sign=private"
    seen = []

    def upstream(_, plugin, path, query, headers):
        seen.append((path, query))
        assert path == "/tg-search"
        return {"list": [{
            "vod_id": "fixture", "vod_name": "Video", "vod_play_from": "Telegram",
            "vod_play_url": url,
        }]}

    app = app_for(tmp_path)
    with patch.object(NativeAtClient, "_request", upstream), TestClient(app) as client:
        def call(module, params):
            if route == "api":
                path = "/t4/api/" + module
                params = {"source": "at", "site": "tg", **params}
            else:
                path = "/t4/fm/at/tg/"
            if method == "GET":
                return client.get(path, params=params)
            return client.post(path, **({"json": params} if method == "JSON" else {"data": params}))

        response = call("detail", {"ac": "detail", "ids": "/v/fixture"})
        assert response.status_code == 200
        label, separator, opaque = response.json()["list"][0]["vod_play_url"].partition("$")
        assert label == "视频" and separator and opaque.startswith("t4_")
        response = call("play", {"play": opaque, "flag": "Telegram"})
        assert response.status_code == 200
        assert response.json() == {"parse": 0, "jx": 0, "url": url, "header": {}}
        assert len(seen) == 1


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("route", ["/t4/fm/at/tg/", "/t4/api/detail"])
def test_upstream_failure_is_actionable_and_redacted_through_gateway(tmp_path, method, route):
    # Exercise the real native response classifier, not a prebuilt exception.
    original = NativeAtClient.__init__

    def initialize(self, *args, **kwargs):
        original(self, transport=httpx.MockTransport(lambda _: httpx.Response(
            400, json={"message": "Invalid share link format https://private.example/secret"},
        )))

    # The application creates native clients during construction.
    with patch.object(NativeAtClient, "__init__", initialize):
        app = app_for(tmp_path)
    params = {"source": "at", "site": "tg", "ac": "detail", "ids": "fixture"}
    with TestClient(app) as client:
        response = client.get(route, params=params) if method == "GET" else client.post(route, json=params)
        assert response.status_code == 502
        error = response.json()["detail"]
        assert error["code"] == "at_upstream_failed"
        assert error["upstream_status"] == 400 and error["reason"] == "invalid_share"
        events = client.get("/t4/api/diagnostics/events").json()["events"]
        assert any(item["stage"] == "at.rejected" and item["reason"] == "invalid_share" for item in events)
        assert "private" not in json.dumps(events) + response.text
