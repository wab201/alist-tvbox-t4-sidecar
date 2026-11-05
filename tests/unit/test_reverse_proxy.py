from urllib.parse import parse_qs, urlsplit

import httpx
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
        config = response.json()
        api = config["sites"][0]["api"]
        parsed = urlsplit(api)
        assert parsed._replace(query="").geturl() == expected_origin + "/t4/fm/at/media/"
        session = parse_qs(parsed.query)["t4_client"][0]
        component_url = response.links["urn:t4-sidecar:client-component"]["url"]
        assert component_url == expected_origin + f"/t4/acceleration/{session}/component.json"
        component = client.get(urlsplit(component_url).path)
        assert component.status_code == 200
        jar = component.json()["spider"]
        assert jar == expected_origin + f"/t4/acceleration/{session}/client.jar"
        assert client.get(urlsplit(jar).path).status_code == 200
        assert set(config) == {"sites"}
        path = parsed._replace(scheme="", netloc="").geturl()
        assert client.get(str(httpx.URL(path).copy_merge_params(
            {"filter": "true"}
        ))).status_code == 200
        assert client.post(path, json={"ac": "list", "t": "all"}).status_code == 200
        assert client.post(path, data={"wd": "test"}).status_code == 200
        assert [call.module for call in adapter.requests] == [
            "home", "category", "search",
        ]
