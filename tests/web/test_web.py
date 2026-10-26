from html.parser import HTMLParser
import subprocess

from fastapi.testclient import TestClient

from sidecar.gateway.main import create_app


def test_t4_web_entry_is_available_without_at_frontend_changes() -> None:
    response = TestClient(create_app()).get("/t4/")

    assert response.status_code == 200
    assert "T4 Sidecar 管理台" in response.text
    assert "/t4/api/sources" in response.text
    assert "http://NAS_IP:8765/t4/" not in response.text
    assert "location.origin}/t4/" in response.text
    assert "T4_GATEWAY_TOKEN" not in response.text
    assert "X-T4-Token" not in response.text
    assert "auth_required" not in response.text
    assert "site=" in response.text
    assert "复制插件 API" in response.text
    assert "复制影视订阅地址" in response.text
    assert "/t4/api/client-config" in response.text
    assert 'id="subscription-url"' in response.text
    assert "可直接添加到 FM 影视" not in response.text
    assert "fm_path" in response.text
    assert "上传并注册" in response.text
    assert "local-uploaded" in response.text
    assert "&t4_token=" not in response.text
    assert "sessionStorage" not in response.text
    assert "secspider" not in response.text.lower()
    assert "刷新运行实例" in response.text
    assert "重命名" in response.text
    assert "plugin_ids" in response.text
    assert 'id="auth-panel"' not in response.text
    assert 'type="password"' not in response.text


def test_web_icons_are_served_locally() -> None:
    client = TestClient(create_app())
    response = client.get("/t4/assets/icons/trash-2.svg")
    assert response.status_code == 200
    assert "<svg" in response.text
    assert client.get("/t4/assets/../registry.json").status_code == 404


def test_management_page_javascript_parses() -> None:
    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active = False
            self.parts = []

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.active = True

        def handle_endtag(self, tag):
            if tag == "script":
                self.active = False

        def handle_data(self, data):
            if self.active:
                self.parts.append(data)

    parser = Scripts()
    parser.feed(TestClient(create_app()).get("/t4/").text)
    assert parser.parts
    checked = subprocess.run(
        ["node", "--check", "-"], input="\n".join(parser.parts),
        text=True, encoding="utf-8", capture_output=True, timeout=10,
    )
    assert checked.returncode == 0, checked.stderr
