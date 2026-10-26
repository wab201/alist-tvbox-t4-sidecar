import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.gateway.uploads import LOCAL_PLUGIN_SOURCE_ID, UploadedPluginStore
from tests.unit.test_uploads import JAVASCRIPT_PLUGIN, PYTHON_PLUGIN


def upload(client, filename="demo.py", content=PYTHON_PLUGIN):
    response = client.post(
        "/t4/api/plugins/upload", files={"file": (filename, content)}
    )
    assert response.status_code == 200, response.text
    return response.json()["plugin"]


def test_manage_plugins_persists_names_order_and_fm_addresses(tmp_path):
    settings = Settings(plugin_dir=str(tmp_path), gateway_token="test")
    with TestClient(create_app(settings, SourceRegistry())) as client:
        client.headers["X-T4-Token"] = "test"
        first = upload(client)
        second = upload(client, "demo.js", JAVASCRIPT_PLUGIN)
        renamed = client.patch(
            f"/t4/api/plugins/{first['id']}", json={"name": " 我的 Python 插件 "}
        )
        assert renamed.status_code == 200
        assert renamed.json()["plugin"]["name"] == "我的 Python 插件"
        assert renamed.json()["plugin"]["fm_path"] == first["fm_path"]
        ids = [second["id"], first["id"]]
        assert client.post("/t4/api/plugins/reorder", json={"plugin_ids": ids}).status_code == 200
        listing = client.get("/t4/api/plugins").json()["plugins"]
        assert [item["id"] for item in listing] == ids
        snapshot = client.get("/t4/api/sources/local-uploaded/subscription").json()
        assert [item["id"] for item in snapshot["plugins"]] == ids
        duplicate = upload(client)
        assert duplicate["name"] == "我的 Python 插件"
        assert duplicate["fm_path"] == first["fm_path"]
        assert len(client.get("/t4/api/plugins").json()["plugins"]) == 2

    with TestClient(create_app(settings, SourceRegistry())) as restarted:
        restarted.headers["X-T4-Token"] = "test"
        restored = restarted.get("/t4/api/plugins").json()["plugins"]
        assert [item["id"] for item in restored] == ids
        assert restored[1]["name"] == "我的 Python 插件"
        assert restored[1]["fm_path"] == first["fm_path"]


@pytest.mark.parametrize("name", ["", "   ", "a" * 81, "a\nb", "a\x00b"])
def test_invalid_name_does_not_change_manifest(tmp_path, name):
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())) as client:
        plugin = upload(client)
        before = (tmp_path / "registry.json").read_bytes()
        assert client.patch(
            f"/t4/api/plugins/{plugin['id']}", json={"name": name}
        ).status_code == 400
        assert (tmp_path / "registry.json").read_bytes() == before


def test_duplicate_name_and_invalid_sort_are_rejected(tmp_path):
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())) as client:
        first = upload(client)
        second = upload(client, "demo.py", PYTHON_PLUGIN + b"\n# second\n")
        assert second["name"] == "demo (2)"
        assert client.patch(
            f"/t4/api/plugins/{second['id']}", json={"name": "DEMO"}
        ).status_code == 400
        for ids in ([], [first["id"]], [first["id"], first["id"]], ["missing", second["id"]]):
            assert client.post(
                "/t4/api/plugins/reorder", json={"plugin_ids": ids}
            ).status_code == 400
        assert [item["id"] for item in client.get("/t4/api/plugins").json()["plugins"]] == [
            first["id"], second["id"]
        ]
        assert client.patch("/t4/api/plugins/missing", json={"name": "New"}).status_code == 404
        assert client.post("/t4/api/plugins/missing/refresh").status_code == 404
        assert client.delete("/t4/api/plugins/missing").status_code == 404


STATEFUL_PYTHON = b"""
import os
class Spider:
    def init(self, extend): self.calls = 0
    def getName(self): return "Stateful"
    def homeContent(self, filter):
        self.calls += 1
        return {"list": [], "calls": self.calls, "pid": os.getpid()}
"""
STATEFUL_JAVASCRIPT = b"""
let calls = 0;
module.exports = {
  getName: () => "Stateful",
  homeContent: () => ({list: [], calls: ++calls, pid: process.pid}),
};
"""


@pytest.mark.parametrize("filename,content", [("state.py", STATEFUL_PYTHON), ("state.js", STATEFUL_JAVASCRIPT)])
def test_refresh_and_delete_reupload_start_fresh_runtime(tmp_path, filename, content):
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())) as client:
        plugin = upload(client, filename, content)
        fm = plugin["fm_path"] + "api/home"
        first = client.get(fm).json()
        assert first["calls"] == 1
        assert client.get(fm).json()["calls"] == 2
        assert client.post(f"/t4/api/plugins/{plugin['id']}/refresh").status_code == 200
        refreshed = client.get(fm).json()
        assert refreshed["calls"] == 1
        assert refreshed["pid"] != first["pid"]
        assert client.delete(f"/t4/api/plugins/{plugin['id']}").status_code == 200
        assert not list(tmp_path.glob("upload_*"))
        assert client.get("/t4/api/plugins").json()["plugins"] == []
        assert client.get(fm).status_code == 404
        assert client.get("/t4/api/sources").json()["sources"] == []
        assert upload(client, filename, content)["fm_path"] + "api/home" == fm
        reuploaded = client.get(fm).json()
        assert reuploaded["calls"] == 1
        assert reuploaded["pid"] != refreshed["pid"]


def test_refresh_checks_current_file_and_reloads_updated_code(tmp_path):
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())) as client:
        plugin = upload(client, content=STATEFUL_PYTHON)
        home_path = plugin["fm_path"] + "api/home"
        assert client.get(home_path).json()["calls"] == 1
        stored = next(tmp_path.glob("upload_*.py"))
        original_stat = stored.stat()
        stored.write_bytes(STATEFUL_PYTHON.replace(b"self.calls = 0", b"self.calls = 9"))
        os.utime(stored, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        assert client.post(f"/t4/api/plugins/{plugin['id']}/refresh").status_code == 200
        assert client.get(home_path).json()["calls"] == 10
        stored.write_bytes(b"class Spider !")
        assert client.post(f"/t4/api/plugins/{plugin['id']}/refresh").status_code == 400
        assert client.get(home_path).json()["calls"] == 11
        stored.unlink()
        assert client.post(f"/t4/api/plugins/{plugin['id']}/refresh").status_code == 400


def test_management_endpoints_respect_optional_protected_mode(tmp_path):
    settings = Settings(plugin_dir=str(tmp_path), gateway_token="test")
    with TestClient(create_app(settings, SourceRegistry())) as client:
        for method, url, body in [
            ("GET", "/t4/api/plugins", None),
            ("POST", "/t4/api/plugins/reorder", {"plugin_ids": []}),
            ("PATCH", "/t4/api/plugins/unknown", {"name": "Name"}),
            ("POST", "/t4/api/plugins/unknown/refresh", None),
            ("DELETE", "/t4/api/plugins/unknown", None),
        ]:
            assert client.request(method, url, json=body).status_code == 401
        assert client.post(
            "/t4/api/plugins/upload", files={"file": ("demo.py", PYTHON_PLUGIN)}
        ).status_code == 401


def test_concurrent_store_uploads_do_not_lose_manifest_entries(tmp_path):
    store = UploadedPluginStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        plugins = list(pool.map(
            lambda index: store.register("demo.py", PYTHON_PLUGIN + f"\n# {index}\n".encode()),
            range(10),
        ))
    snapshot = UploadedPluginStore(tmp_path).snapshot_for(LOCAL_PLUGIN_SOURCE_ID)
    assert {item.plugin_id for item in snapshot.plugins} == {item.plugin_id for item in plugins}
    assert len({item.name for item in snapshot.plugins}) == 10
    assert len(json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))["plugins"]) == 10


@pytest.mark.parametrize("filename,content", [("demo.py", PYTHON_PLUGIN), ("demo.js", JAVASCRIPT_PLUGIN)])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_fm_six_method_contract_after_management(tmp_path, filename, content, method):
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())) as client:
        plugin = upload(client, filename, content)
        assert client.patch(f"/t4/api/plugins/{plugin['id']}", json={"name": "测试源"}).status_code == 200
        assert client.post(f"/t4/api/plugins/{plugin['id']}/refresh").status_code == 200

        def call(module, **params):
            options = {"params": params} if method == "GET" else {"json": params}
            response = client.request(method, plugin["fm_path"] + "api/" + module, **options)
            assert response.status_code == 200, response.text
            return response.json()

        home = call("home")
        assert home["list"][0]["vod_id"].startswith("t4_")
        assert call("category", t="all", pg="2")["page"] == 2
        assert call("search", wd="demo", pg="3")["page"] == 3
        detail = call("detail", ids=home["list"][0]["vod_id"])
        play_id = detail["list"][0]["vod_play_url"].split("$", 1)[1]
        assert call("play", flag="Uploaded", play=play_id)["url"].startswith("https://")
        assert call("action", action="inspect")["action"] == "inspect"


@pytest.mark.parametrize("filename,content", [
    ("log.py", b'print("loading")\n' + PYTHON_PLUGIN.replace(
        b'return "Uploaded Python"', b'print("name"); return "Uploaded Python"'
    )),
    ("log.js", b'console.log("loading");\n' + JAVASCRIPT_PLUGIN.replace(
        b'init: () => "upload-js-init-ok"', b'init: () => { console.log("init"); return "ok"; }'
    )),
])
def test_plugin_diagnostics_do_not_corrupt_worker_protocol(tmp_path, filename, content):
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())) as client:
        plugin = upload(client, filename, content)
        response = client.get(plugin["fm_path"] + "api/home")
        assert response.status_code == 200, response.text
        assert response.json()["list"]
