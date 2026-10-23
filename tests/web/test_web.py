from fastapi.testclient import TestClient

from sidecar.gateway.main import create_app


def test_t4_web_entry_is_available_without_at_frontend_changes() -> None:
    response = TestClient(create_app()).get("/t4/")

    assert response.status_code == 200
    assert "T4 Sidecar 管理台" in response.text
    assert "/t4/api/sources" in response.text
    assert "http://NAS_IP:8765/t4/" not in response.text
    assert "location.origin}/t4/" in response.text
    assert "T4_GATEWAY_TOKEN" in response.text
    assert "X-T4-Token" in response.text
    assert "auth_required" in response.text
    assert "site=" in response.text
    assert "复制 FM 地址" in response.text
    assert "fm_path" in response.text
    assert "上传并注册" in response.text
    assert "local-uploaded" in response.text
    assert "&t4_token=" not in response.text
    assert "sessionStorage" in response.text
    assert "secspider" not in response.text.lower()
