from fastapi.testclient import TestClient

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app


def test_health_returns_stable_public_status() -> None:
    client = TestClient(
        create_app(
            Settings(
                service_name="test-sidecar",
                version="test-version",
                worker_status="not_started",
            )
        )
    )

    response = client.get("/t4/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "test-sidecar",
        "status": "ok",
        "version": "test-version",
        "worker": {"status": "not_started"},
    }


def test_health_does_not_expose_unrelated_runtime_values() -> None:
    settings = Settings.from_env()
    client = TestClient(create_app(settings))

    payload = client.get("/t4/health").json()

    assert "token" not in payload
    assert "secret" not in payload
    assert "keyring" not in payload
