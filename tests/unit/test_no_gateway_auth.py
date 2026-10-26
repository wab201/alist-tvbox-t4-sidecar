import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from sidecar.at.subscription import AtSubscriptionClient
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from tests.unit.test_client_config import fetcher
from tests.unit.test_fm import FakeAdapter


@pytest.mark.parametrize("legacy_token", ["", "obsolete-gateway"])
def test_legacy_gateway_env_cannot_enable_extra_auth(tmp_path, monkeypatch, legacy_token):
    monkeypatch.setenv("T4_GATEWAY_TOKEN", legacy_token)
    monkeypatch.setenv("T4_PLUGIN_DIR", str(tmp_path))
    registry = SourceRegistry((SourceConfig(
        "env-at", "AT", subscription_url="https://at.example/sub/private-sub/1"
    ),))
    app = create_app(
        Settings.from_env(), registry, subscription_fetcher=fetcher, t4_adapter=FakeAdapter()
    )
    with TestClient(app) as client:
        for path in (
            "/t4/", "/t4/health", "/t4/config.json", "/t4/api/client-config",
            "/t4/api/sources", "/t4/api/sources/env-at/subscription",
            "/t4/api/plugins", "/t4/api/home?source=env-at&site=bili",
            "/t4/fm/env-at/bili/", "/t4/fm/env-at/bili/api/home",
        ):
            assert client.get(path).status_code == 200, path
        routes = [route for route in app.routes if isinstance(route, APIRoute)]
        assert not any(
            field.alias == "X-T4-Token"
            for route in routes for field in route.dependant.header_params
        )
        assert not any("access_key" in route.path for route in routes)
        assert not any("gateway" in name for name in Settings.__dataclass_fields__)


@pytest.mark.parametrize("upstream_status,expected_status", [(200, 200), (401, 502)])
def test_removing_gateway_auth_preserves_at_subscription_authorization(
    tmp_path, upstream_status, expected_status
):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            upstream_status, json={"sites": [{"key": "bili", "api": "csp_BiliBili"}]}
        )

    upstream = AtSubscriptionClient(transport=httpx.MockTransport(handler))
    registry = SourceRegistry((SourceConfig(
        "env-at", "AT", at_base_url="https://at.example",
        subscription_id="1", subscription_token="upstream-token",
    ),))
    app = create_app(
        Settings(plugin_dir=str(tmp_path)), registry,
        subscription_fetcher=upstream.fetch, t4_adapter=FakeAdapter(),
    )
    with TestClient(app) as client:
        response = client.get("/t4/config.json")
    assert response.status_code == expected_status
    assert len(seen) == 1
    assert seen[0].url.path == "/sub/upstream-token/1"
    assert "upstream-token" not in response.text
