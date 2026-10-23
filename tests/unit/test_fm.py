from fastapi.testclient import TestClient

from sidecar.gateway.config import Settings
from sidecar.gateway.fm import build_fm_access_key, build_fm_path
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.adapter import T4Request


class FakeAdapter:
    def __init__(self) -> None:
        self.requests: list[T4Request] = []

    async def dispatch(self, request: T4Request) -> dict[str, object]:
        self.requests.append(request)
        return {"class": "t4", "list": []}


def test_fm_path_is_derived_without_exposing_gateway_token() -> None:
    path = build_fm_path("gateway-token", "env-at", "csp_BiliBili")

    assert path is not None
    assert path.startswith("/t4/fm/env-at/csp_BiliBili/")
    assert "gateway-token" not in path
    assert build_fm_access_key("gateway-token", "env-at", "csp_BiliBili") in path


def test_fm_route_injects_source_and_plugin_without_client_parameters() -> None:
    adapter = FakeAdapter()
    source = SourceConfig("env-at", "AT subscription", status="configured")
    app = create_app(
        Settings(gateway_token="gateway-token"),
        SourceRegistry((source,)),
        t4_adapter=adapter,
    )
    access_key = build_fm_access_key("gateway-token", "env-at", "csp_BiliBili")
    response = TestClient(app).get(
        f"/t4/fm/env-at/csp_BiliBili/{access_key}/api/home"
    )

    assert response.status_code == 200
    assert adapter.requests == [
        T4Request(
            module="home",
            source_id="env-at",
            plugin_id="csp_BiliBili",
            params={},
        )
    ]


def test_fm_route_rejects_invalid_access_key() -> None:
    source = SourceConfig("env-at", "AT subscription", status="configured")
    app = create_app(
        Settings(gateway_token="gateway-token"),
        SourceRegistry((source,)),
        t4_adapter=FakeAdapter(),
    )

    response = TestClient(app).get(
        "/t4/fm/env-at/csp_BiliBili/invalid-key/api/home"
    )

    assert response.status_code == 401
