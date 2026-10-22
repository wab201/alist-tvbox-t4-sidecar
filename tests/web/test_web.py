from fastapi.testclient import TestClient

from sidecar.gateway.main import create_app


def test_t4_web_entry_is_available_without_at_frontend_changes() -> None:
    response = TestClient(create_app()).get("/t4/")

    assert response.status_code == 200
    assert "T4 Sidecar" in response.text
    assert "/t4/api/sources" in response.text
    assert "secspider" not in response.text.lower()
