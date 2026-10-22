import json

from fastapi.testclient import TestClient

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry


def make_client() -> TestClient:
    settings = Settings(gateway_token="gateway-test-token")
    registry = SourceRegistry(
        (
            SourceConfig(
                source_id="demo",
                display_name="Demo source",
                status="ready",
                capabilities=("raw", "t4"),
            ),
        )
    )
    return TestClient(create_app(settings, registry))


def test_source_list_requires_gateway_token() -> None:
    client = make_client()

    response = client.get("/t4/api/sources")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthorized"


def test_source_list_returns_public_metadata_only() -> None:
    client = make_client()

    response = client.get(
        "/t4/api/sources",
        headers={"X-T4-Token": "gateway-test-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "sources": [
            {
                "id": "demo",
                "name": "Demo source",
                "status": "ready",
                "capabilities": ["raw", "t4"],
            }
        ]
    }
    assert "token" not in response.text
    assert "secret" not in response.text
    assert "keyring" not in response.text


def test_source_registry_reads_safe_metadata_file(tmp_path) -> None:
    source_file = tmp_path / "sources.json"
    source_file.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "file-source",
                        "name": "File source",
                        "status": "configured",
                        "capabilities": ["raw"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = SourceRegistry.from_file(str(source_file))

    assert registry.public_metadata() == [
        {
            "id": "file-source",
            "name": "File source",
            "status": "configured",
            "capabilities": ["raw"],
        }
    ]
