import ast
import base64
import json
from types import SimpleNamespace

import pytest
import requests

from sidecar.worker.atvp_runtime import AtvpRuntime
from tests.worker.test_secspider_runtime import build_secspider_fixture
from tests.worker.test_secspider_runtime import VENDOR_ATVP


SOURCE = '''
from base.spider import Spider as Base
import json
class Spider(Base):
    backend_parse = False
    def init(self, extend):
        self.options = json.loads(extend)
    def getName(self): return "Cloud fixture"
    def homeContent(self, filter):
        return {"list": [{"vod_id": provider} for provider in self.options["config_keys"]]}
'''


@pytest.mark.parametrize("encrypted", [False, True], ids=["raw", "secspider"])
def test_server_proxy_keeps_inner_provider_config_and_only_changes_at_play_request(tmp_path, monkeypatch, encrypted):
    path = tmp_path / ("plugin.txt" if encrypted else "plugin.py")
    loader = ""
    if encrypted:
        path, loader, _, _ = build_secspider_fixture(tmp_path, SOURCE)
    else:
        path.write_text(SOURCE, encoding="utf-8")
    configs = {"PAN115": {"enabled": True, "concurrency": 2, "chunk_size": 1024}}
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        return SimpleNamespace(status_code=200, text=json.dumps({
            "url": "https://at.example/p/fixture-token/1@99", "header": {},
        }))

    monkeypatch.setattr(requests, "get", get)
    runtime = AtvpRuntime()
    try:
        runtime.init(base64.b64encode(json.dumps({
            "api": "https://at.example", "source": str(path),
            "loader": loader, "raw": not encrypted, "token": "fixture-token",
            "secspider_loader": "self",
            "local_proxy_config": configs, "_t4_server_proxy": True,
            "data": {"config_keys": list(configs)},
        }).encode()).decode())
        assert runtime.home()["list"] == [{"vod_id": "PAN115"}]
        assert runtime._spider._localProxyConfig == configs
        assert ast.literal_eval(runtime._spider._inner.options["local_proxy_config"]) == configs
        assert runtime.play("PAN115", "1@99", [])["url"].endswith("/1@99")
        assert calls == [("https://at.example/play/fixture-token", {"id": "1@99", "type": ""})]
        runtime._spider.fetch("https://plugin.example/api", params={"type": "client-proxy"})
        assert calls[-1] == ("https://plugin.example/api", {"type": "client-proxy"})
    finally:
        runtime.close()


def test_newer_atvp_local_proxy_discovery_is_disabled_without_clearing_config():
    runtime = AtvpRuntime()
    configs = {"PAN115": {"enabled": True}}
    calls = []
    runtime._spider = SimpleNamespace(
        _load_inner_spider_class=lambda _: None,
        _build_backend_endpoint=lambda _: "https://at.example/play/token",
        _localProxyConfig=configs,
        _resolve_local_proxy_base=lambda: calls.append("scan"),
        fetch=lambda *args, **kwargs: (args, kwargs),
    )
    try:
        runtime._configure_server_proxy()
        assert runtime._spider._localProxyConfig is configs
        assert runtime._spider._resolve_local_proxy_base() is None and calls == []
        _, values = runtime._spider.fetch(
            "https://at.example/play/token", params={"id": "1@99", "type": "client-proxy"},
        )
        assert values["params"] == {"id": "1@99", "type": ""}
    finally:
        runtime._spider = None
        runtime.close()


@pytest.mark.parametrize("server_proxy", [False, True])
def test_device_port_scan_is_disabled_in_both_direct_and_server_modes(tmp_path, server_proxy):
    path = tmp_path / "plugin.py"
    path.write_text(SOURCE, encoding="utf-8")
    loader = tmp_path / "loader.py"
    loader.write_text(VENDOR_ATVP.read_text(encoding="utf-8") + """
def unexpected_scan(self):
    raise AssertionError("server must not scan a device proxy")
Spider._resolve_local_proxy_base = unexpected_scan
""", encoding="utf-8")
    runtime = AtvpRuntime()
    configs = {"PAN115": {"enabled": True}}
    try:
        runtime.init(base64.b64encode(json.dumps({
            "api": "https://at.example", "source": str(path), "loader": str(loader),
            "raw": True, "token": "fixture", "local_proxy_config": configs,
            "_t4_server_proxy": server_proxy, "data": {"config_keys": list(configs)},
        }).encode()).decode())
        assert runtime._spider._resolve_local_proxy_base() is None
        assert runtime._spider._localProxyConfig == configs
    finally:
        runtime.close()
