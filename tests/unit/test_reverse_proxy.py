from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from uvicorn import Config

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from tests.unit.test_fm import FakeAdapter


@pytest.mark.parametrize(
    "origin,forwarded_proto,expected_origin",
    [
        ("http://nas.test:8765", None, "http://nas.test:8765"),
        ("http://t4.example.test", "https", "https://t4.example.test"),
        (
            "http://t4.example.test:15443",
            "https",
            "https://t4.example.test:15443",
        ),
        (
            "http://[2001:db8::1]:15443",
            "https",
            "https://[2001:db8::1]:15443",
        ),
    ],
)
def test_proxy_subscription_preserves_external_origin(
    tmp_path, monkeypatch, origin, forwarded_proto, expected_origin
):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    adapter = FakeAdapter()

    async def subscription(_source):
        return {
            "sites": [
                {"key": "media", "name": "Media", "api": "csp_Media"},
            ]
        }

    app = create_app(
        Settings(plugin_dir=str(tmp_path)),
        SourceRegistry(
            (SourceConfig("at", "AT", subscription_url="https://at.example/sub/test/1"),)
        ),
        subscription_fetcher=subscription,
        t4_adapter=adapter,
    )
    server = Config(app, log_config=None, interface="asgi3", lifespan="off")
    server.load()
    headers = {"Host": urlsplit(origin).netloc}
    if forwarded_proto:
        headers["X-Forwarded-Proto"] = forwarded_proto
    with TestClient(
        server.loaded_app,
        base_url="http://sidecar.test:8765",
        client=("172.18.0.1", 50000),
        headers=headers,
    ) as client:
        response = client.get("/t4/config.json")
        assert response.status_code == 200
        api = response.json()["sites"][0]["api"]
        assert api == expected_origin + "/t4/fm/at/media/"
        path = urlsplit(api).path
        assert client.get(path, params={"filter": "true"}).status_code == 200
        assert client.post(path, json={"ac": "list", "t": "all"}).status_code == 200
        assert client.post(path, data={"wd": "test"}).status_code == 200
        assert [call.module for call in adapter.requests] == [
            "home", "category", "search",
        ]
