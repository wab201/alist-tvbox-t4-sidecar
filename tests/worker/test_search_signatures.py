import base64
import json

import pytest
from fastapi.testclient import TestClient

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.worker.atvp_runtime import AtvpRuntime, AtvpRuntimeError
from tests.worker.test_secspider_runtime import build_secspider_fixture


@pytest.mark.parametrize("mode", ["direct", "raw", "secspider"])
@pytest.mark.parametrize("signature", ["pg=False", "page=1", "quick=False", 'quick, pg="1"', "*args"])
def test_search_signature_preserves_page_and_quick(tmp_path, mode, signature):
    if signature == "*args":
        result = '{"page": int(args[1]), "quick": args[0]}'
    elif signature.startswith("quick,"):
        result = '{"page": int(pg), "quick": quick}'
    elif signature.startswith("quick="):
        result = '{"page": 1, "quick": quick}'
    else:
        name = signature.partition("=")[0]
        result = '{"page": int(' + name + ')}'
    source = (
        "class Spider:\n"
        "    def init(self, extend): pass\n"
        '    def getName(self): return "Search fixture"\n'
        "    def searchContent(self, key, " + signature + "):\n"
        "        return dict(" + result + ', list=[{"vod_id": key, "vod_name": key}])\n'
    )
    path = tmp_path / "search.py"
    path.write_text(source, encoding="utf-8")
    payload = {"loader": str(path)} if mode == "direct" else {
        "source": str(path), "raw": True,
    }
    if mode == "secspider":
        package, loader, _, _ = build_secspider_fixture(tmp_path, source)
        payload.update(source=package, loader=loader, raw=False, secspider_loader="self")
    runtime = AtvpRuntime()
    try:
        runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
        for quick, page in ((False, "2"), (True, "3")):
            value = runtime.search("Fixture", quick, page)
            assert value["page"] == (1 if signature == "quick=False" else int(page))
            assert value["list"][0]["vod_name"] == "Fixture"
            if "quick" in value:
                assert value["quick"] is quick
    finally:
        runtime.close()


@pytest.mark.parametrize("mode", ["direct", "raw", "secspider"])
def test_paginated_search_does_not_retry_internal_type_error(tmp_path, mode):
    source = """
class Spider:
    calls = 0
    def init(self, extend): pass
    def getName(self): return "Search error fixture"
    def searchContent(self, key, pg=False):
        self.calls += 1
        raise TypeError("internal failure")
"""
    path = tmp_path / "search.py"
    path.write_text(source, encoding="utf-8")
    payload = {"loader": str(path)} if mode == "direct" else {
        "source": str(path), "raw": True,
    }
    if mode == "secspider":
        package, loader, _, _ = build_secspider_fixture(tmp_path, source)
        payload.update(source=package, loader=loader, raw=False, secspider_loader="self")
    runtime = AtvpRuntime()
    try:
        runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
        with pytest.raises(AtvpRuntimeError, match="TypeError"):
            runtime.search("Fixture", False, "2")
        spider = getattr(runtime._spider, "_inner", runtime._spider)
        assert spider.calls == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("mode", ["direct", "raw"])
def test_plain_spider_without_get_name_can_initialize(tmp_path, mode):
    path = tmp_path / "plain.py"
    path.write_text("""
class Spider:
    def init(self, extend): return "ready"
    def searchContent(self, key, pg=False):
        return {"page": int(pg), "list": []}
""", encoding="utf-8")
    payload = {"loader": str(path)} if mode == "direct" else {
        "source": str(path), "raw": True,
    }
    runtime = AtvpRuntime()
    try:
        result = runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
        assert result["result"] == "ready"
        assert result["name"]
        if mode == "direct":
            assert result["name"] == "plain"
        assert runtime.search("Fixture", False, "2")["page"] == 2
    finally:
        runtime.close()


def test_existing_get_name_errors_are_not_silenced(tmp_path):
    path = tmp_path / "broken_name.py"
    path.write_text("""
class Spider:
    def init(self, extend): pass
    def getName(self): raise ValueError("plugin name failure")
""", encoding="utf-8")
    runtime = AtvpRuntime()
    try:
        extend = base64.b64encode(json.dumps({"loader": str(path)}).encode()).decode()
        with pytest.raises(ValueError, match="plugin name failure"):
            runtime.init(extend)
    finally:
        runtime.close()


@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_plain_uploaded_spider_paginates_through_real_worker_and_http(tmp_path, transport):
    source = b"""
class Spider:
    def init(self, extend): pass
    def searchContent(self, key, pg=False):
        return {"page": int(pg), "list": [{"vod_id": str(pg), "vod_name": key}]}
"""
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with TestClient(app) as client:
        uploaded = client.post("/t4/api/plugins/upload", files={"file": ("plain.py", source)})
        assert uploaded.status_code == 200
        api = uploaded.json()["plugin"]["fm_path"]
        params = {"wd": "Fixture", "pg": "2", "quick": True}
        if transport == "GET":
            response = client.get(api, params=params)
        elif transport == "JSON":
            response = client.post(api, json=params)
        else:
            response = client.post(api, data=params)
        assert response.status_code == 200, response.text
        assert response.json()["page"] == 2
        assert response.json()["list"][0]["vod_name"] == "Fixture"
