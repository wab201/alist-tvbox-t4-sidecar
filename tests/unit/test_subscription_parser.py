import base64
import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from sidecar.at.subscription import (
    AtSubscriptionClient,
    SubscriptionParseError,
    SubscriptionParser,
    build_subscription_url,
)
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry


AT_BASE = "http://at.example.test:4567"
AT_LOADER = f"{AT_BASE}/Atvp/token/v1.py"
LEGACY_AT_LOADER = f"{AT_BASE}/Atvp.py"


def encoded_ext(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def test_parser_decodes_at_ext_and_classifies_plugin_without_public_secrets() -> None:
    parser = SubscriptionParser(AT_BASE)
    snapshot = parser.parse(
        "demo",
        {
            "sites": [
                {
                    "key": "DemoPlugin",
                    "name": "Demo plugin",
                    "api": "csp_PyProxy",
                    "type": 3,
                    "ext": encoded_ext(
                        {
                            "api": AT_BASE,
                            "source": f"{AT_BASE}/plugins/sub/42.txt",
                            "loader": AT_LOADER,
                            "token": "subscription-token",
                            "secret": "private-secret",
                            "raw": False,
                            "local_proxy_config": {"demo": {"enabled": True}},
                            "filters": [{"name": "detail-normalizer"}],
                        }
                    ),
                }
            ]
        },
    )

    assert snapshot.public_metadata() == {
        "source_id": "demo",
        "site_count": 1,
        "plugins": [
            {
                "id": "DemoPlugin",
                "name": "Demo plugin",
                "type": "secspider",
                "api": "csp_PyProxy",
                "raw": False,
                "filters": 1,
            }
        ],
        "errors": [],
    }
    assert "subscription-token" not in json.dumps(snapshot.public_metadata())
    assert "private-secret" not in json.dumps(snapshot.public_metadata())


def test_parser_classifies_raw_python_and_keeps_builtin_out_of_plugins() -> None:
    parser = SubscriptionParser(AT_BASE)
    snapshot = parser.parse(
        "demo",
        {
            "sites": [
                {"key": "native", "name": "Native", "api": "csp_AList"},
                {
                    "key": "RawPlugin",
                    "api": "csp_PyProxy",
                    "ext": {
                        "source": f"{AT_BASE}/plugins/sub/7.py",
                        "loader": AT_LOADER,
                        "raw": True,
                    },
                },
            ]
        },
    )

    assert [item.plugin_type for item in snapshot.plugins] == ["raw_python"]
    assert snapshot.site_count == 2


def test_parser_accepts_legacy_same_origin_atvp_loader() -> None:
    parser = SubscriptionParser(AT_BASE)

    snapshot = parser.parse(
        "demo",
        {
            "sites": [
                {
                    "key": "legacy",
                    "api": "csp_PyProxy",
                    "ext": {
                        "source": f"{AT_BASE}/plugins/sub/7.py",
                        "loader": LEGACY_AT_LOADER,
                        "raw": True,
                    },
                }
            ]
        },
    )

    assert snapshot.errors == ()
    assert snapshot.plugins[0].loader == LEGACY_AT_LOADER


def test_parser_rejects_loader_outside_at_origin() -> None:
    parser = SubscriptionParser(AT_BASE)

    snapshot = parser.parse(
        "demo",
        {
            "sites": [
                {
                    "key": "bad",
                    "ext": {
                        "source": f"{AT_BASE}/plugins/1.txt",
                        "loader": "https://other.example/Atvp/token/v1.py",
                    },
                }
            ]
        },
    )

    assert snapshot.plugins == ()
    assert snapshot.errors == (
        "site[0]: Atvp loader is outside the configured AT origin",
    )


def test_subscription_url_uses_tokenized_at_route() -> None:
    source = SourceConfig(
        source_id="demo",
        display_name="Demo",
        at_base_url=AT_BASE,
        subscription_id="12",
        subscription_token="token-value",
    )

    assert build_subscription_url(source) == f"{AT_BASE}/sub/token-value/12"


def test_subscription_url_accepts_complete_subscription_url() -> None:
    subscription_url = f"{AT_BASE}/sub/token-value/Chajian"
    source = SourceConfig(
        source_id="demo",
        display_name="Demo",
        subscription_url=subscription_url,
    )

    assert build_subscription_url(source) == subscription_url


def test_subscription_client_requests_at_sub_route() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"sites": []})

    source = SourceConfig(
        source_id="demo",
        display_name="Demo",
        at_base_url=AT_BASE,
        subscription_id="12",
        subscription_token="token-value",
    )

    payload = asyncio.run(
        AtSubscriptionClient(httpx.MockTransport(handler)).fetch(source)
    )

    assert payload == {"sites": []}
    assert len(requests) == 1
    assert str(requests[0].url) == f"{AT_BASE}/sub/token-value/12"


def test_subscription_client_redacts_transport_failures() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    source = SourceConfig(
        source_id="demo",
        display_name="Demo",
        at_base_url=AT_BASE,
        subscription_id="12",
        subscription_token="private-token",
    )

    try:
        asyncio.run(
            AtSubscriptionClient(httpx.MockTransport(handler)).fetch(source)
        )
    except SubscriptionParseError as exc:
        assert str(exc) == "AT subscription request failed before receiving a response"
    else:
        raise AssertionError("expected a normalized subscription error")


async def fake_fetcher(source: SourceConfig) -> dict[str, object]:
    assert source.subscription_token == "token-value"
    return {
        "sites": [
            {
                "key": "RawPlugin",
                "name": "Raw plugin",
                "api": "csp_PyProxy",
                "ext": {
                    "source": f"{AT_BASE}/plugins/sub/7.py",
                    "loader": AT_LOADER,
                    "raw": True,
                },
            }
        ]
    }


def test_subscription_route_returns_redacted_snapshot() -> None:
    source = SourceConfig(
        source_id="demo",
        display_name="Demo",
        at_base_url=AT_BASE,
        subscription_id="12",
        subscription_token="token-value",
    )
    app = create_app(
        Settings(gateway_token="gateway-test-token"),
        SourceRegistry((source,)),
        fake_fetcher,
    )

    response = TestClient(app).get(
        "/t4/api/sources/demo/subscription",
        headers={"X-T4-Token": "gateway-test-token"},
    )

    assert response.status_code == 200
    assert response.json()["plugins"][0]["type"] == "raw_python"
    assert "token-value" not in response.text


def test_app_registers_environment_subscription_when_source_file_is_missing() -> None:
    settings = Settings(
        gateway_token="gateway-test-token",
        source_config_file="missing-sources.json",
        subscription_url=f"{AT_BASE}/sub/token-value/Chajian",
    )
    app = create_app(settings, t4_adapter=object())

    response = TestClient(app).get(
        "/t4/api/sources",
        headers={"X-T4-Token": "gateway-test-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "sources": [
            {
                "id": "env-at",
                "name": "AT environment subscription",
                "status": "configured",
                "capabilities": ["subscription", "t4"],
            }
        ]
    }
