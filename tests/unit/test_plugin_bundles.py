import io
import json
from pathlib import Path
import zipfile

from fastapi.testclient import TestClient
import pytest

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.gateway.uploads import PluginUploadError, UploadedPluginStore
from sidecar.worker.atvp_runtime import AtvpRuntime
from sidecar.worker.client import WorkerPool


MAIN = """
from base.spider import Spider as Base
from shared_helper import title
class Spider(Base):
    def init(self, extend): return "ready"
    def getName(self): return title
    def homeContent(self, filter):
        return {"list": [{"vod_id": "1", "vod_name": title}], "class": []}
"""


def bundle(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            entry = zipfile.ZipInfo()
            entry.filename = name
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)
    return output.getvalue()


def test_bundle_upload_executes_shared_import_and_survives_reload_and_delete(tmp_path):
    payload = bundle({"main.py": MAIN, "shared_helper.py": 'title = "Bundled title"'})
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with TestClient(app) as client:
        uploaded = client.post("/t4/api/plugins/upload", files={"file": ("demo.zip", payload)})
        assert uploaded.status_code == 200, uploaded.text
        plugin = uploaded.json()["plugin"]
        home = client.get(plugin["fm_path"])
        assert home.status_code == 200, home.text
        assert home.json()["list"][0]["vod_name"] == "Bundled title"
        refreshed = client.post(f"/t4/api/plugins/{plugin['id']}/refresh")
        assert refreshed.status_code == 200
        reloaded = UploadedPluginStore(tmp_path).snapshot_for("local-uploaded").plugins
        assert len(reloaded) == 1
        assert Path(reloaded[0].loader).parent.name == plugin["id"]
        assert client.delete(f"/t4/api/plugins/{plugin['id']}").status_code == 200
        assert not (tmp_path / plugin["id"]).exists()


def test_bundle_entry_manifest_and_dependency_files_use_isolated_worker(tmp_path):
    store = UploadedPluginStore(tmp_path)
    plugin = store.register("bundle.zip", bundle({
        "plugin.json": json.dumps({"entry": "crawler/entry.py"}),
        "crawler/entry.py": MAIN,
        "crawler/shared_helper.py": 'title = "Nested title"',
    }))
    with WorkerPool() as pool:
        assert pool.request("bundle", "init", {"extend": plugin.worker_extend()})["name"] == "Nested title"
        assert pool.request("bundle", "home")["list"][0]["vod_name"] == "Nested title"
    assert UploadedPluginStore(tmp_path).has_plugins()


def test_bundle_local_helper_has_priority_over_shared_common_module(tmp_path, monkeypatch):
    common = tmp_path / "common"
    common.mkdir()
    (common / "shared_helper.py").write_text('title = "Wrong shared title"', encoding="utf-8")
    monkeypatch.setenv("T4_PLUGIN_DIR", str(tmp_path))
    plugin = UploadedPluginStore(tmp_path).register("bundle.zip", bundle({
        "main.py": MAIN, "shared_helper.py": 'title = "Own bundled title"',
    }))
    with WorkerPool() as pool:
        assert pool.request("priority", "init", {"extend": plugin.worker_extend()})["name"] == "Own bundled title"


def test_bundle_can_infer_unique_spider_among_helper_modules(tmp_path):
    plugin = UploadedPluginStore(tmp_path).register("bundle.zip", bundle({
        "crawler.py": MAIN, "shared_helper.py": 'title = "Inferred"',
    }))
    assert Path(plugin.loader).name == "crawler.py"


@pytest.mark.parametrize("files", [
    {"../outside.py": MAIN},
    {"/outside.py": MAIN},
    {"C:/outside.py": MAIN},
    {"folder\\outside.py": MAIN},
    {"main.py": MAIN, "plugin.json": '{"entry":"../outside.py"}'},
    {"main.py": MAIN, "plugin.json": '{"entry":"/outside.py"}'},
    {"main.py": MAIN, "plugin.json": '{"entry":"registry.json"}'},
    {"main.py": MAIN, "plugin.json": '{"entry":"main.py\\u0000"}'},
    {"module": "text", "module/entry.py": MAIN},
    {"module/entry.py": MAIN, "module": "text"},
    {"first.py": MAIN, "second.py": MAIN},
    {"module.py": "x = 1"},
    {"main.py": "invalid syntax !"},
])
def test_invalid_bundle_does_not_register_or_write_outside_store(tmp_path, files):
    root = tmp_path / "plugins"
    store = UploadedPluginStore(root)
    with pytest.raises(PluginUploadError):
        store.register("invalid.zip", bundle(files))
    assert not store.has_plugins()
    assert not (tmp_path / "outside.py").exists()
    assert not list(root.glob(".upload-*"))
    assert list(root.iterdir()) == []


def test_bundle_size_limit_applies_to_expanded_contents(tmp_path):
    store = UploadedPluginStore(tmp_path, max_bytes=1000)
    with pytest.raises(PluginUploadError):
        store.register("big.zip", bundle({"main.py": MAIN, "large.bin": b"x" * 9000}))
    assert not store.has_plugins()


def test_bundle_without_local_helper_produces_explicit_module_diagnostic(tmp_path):
    store = UploadedPluginStore(tmp_path)
    plugin = store.register("missing.zip", bundle({"main.py": MAIN.replace("shared_helper", "t4_absent_helper")}))
    from sidecar.worker.client import WorkerRemoteError
    with WorkerPool() as pool, pytest.raises(WorkerRemoteError) as error:
        pool.request("missing", "init", {"extend": plugin.worker_extend()})
    assert error.value.details["missing_module"] == "t4_absent_helper"


def test_javascript_bundle_executes_its_own_helper(tmp_path):
    store = UploadedPluginStore(tmp_path)
    plugin = store.register("javascript.zip", bundle({
        "index.js": """
const { title } = require("./helper.js");
module.exports = {
    init() { return "ready"; },
    getName() { return title; },
    homeContent() { return { list: [{ vod_id: "1", vod_name: title }] }; }
};
""",
        "helper.js": 'module.exports = { title: "JavaScript bundle" };',
    }))
    with WorkerPool() as pool:
        assert pool.request("js-bundle", "init", {"extend": plugin.worker_extend()})["name"] == "JavaScript bundle"
        assert pool.request("js-bundle", "home")["list"][0]["vod_name"] == "JavaScript bundle"


def test_missing_module_is_shown_in_public_api_and_gate_evidence(tmp_path):
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with TestClient(app) as client:
        uploaded = client.post("/t4/api/plugins/upload", files={"file": (
            "broken.py", MAIN.replace("shared_helper", "t4_absent_helper").encode(),
        )})
        plugin = uploaded.json()["plugin"]
        for path, params in (
            (plugin["fm_path"], {}),
            ("/t4/api/home", {"source": "local-uploaded", "site": plugin["id"]}),
        ):
            response = client.get(path, params=params)
            assert response.status_code == 502
            error = response.json()["detail"]
            assert error["missing_module"] == "t4_absent_helper"
            assert "t4_absent_helper" in error["message"]
            assert str(tmp_path) not in response.text
