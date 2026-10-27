import base64
import io
import json
import sys
from unittest.mock import patch

import pytest

from sidecar.diagnostics import TraceStore, trace_scope
from sidecar.worker.atvp_runtime import AtvpRuntime
from sidecar.worker.client import WorkerPool, WorkerRemoteError
from sidecar.worker.errors import error_details
from sidecar.worker import process


def encoded(payload):
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_unexpected_worker_failure_preserves_request_id_and_never_exports_exception_text():
    request = {"id": "known-id", "method": "init", "params": {}}
    missing = ModuleNotFoundError("https://private.example/token?secret=value", name="shared_helper")
    output = io.StringIO()
    with (
        patch.object(process.ATVP_RUNTIME, "init", side_effect=missing),
        patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")),
        patch.object(sys, "stdout", output),
    ):
        assert process.main() == 0
    response = json.loads(output.getvalue())
    assert response["id"] == request["id"]
    assert response["error"]["code"] == "plugin_init_failed"
    assert response["error"]["missing_module"] == "shared_helper"
    assert response["error"]["root_error"] == "ModuleNotFoundError"
    assert "private.example" not in output.getvalue()
    assert "secret=value" not in output.getvalue()


def test_outer_worker_failure_also_preserves_request_id():
    output = io.StringIO()
    with (
        patch.object(process, "_handle", side_effect=RuntimeError("private-cookie")),
        patch.object(sys, "stdin", io.StringIO('{"id":"outer","method":"ping"}\n')),
        patch.object(sys, "stdout", output),
    ):
        process.main()
    result = json.loads(output.getvalue())
    assert result["id"] == "outer"
    assert result["error"]["code"] == "worker_error"
    assert "private-cookie" not in output.getvalue()


def test_actual_worker_missing_module_is_actionable_not_protocol_error(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("import absent_t4_fixture_module\nclass Spider: pass", encoding="utf-8")
    store = TraceStore()
    with WorkerPool() as pool, trace_scope(store, trace_id="missing"):
        with pytest.raises(WorkerRemoteError) as raised:
            pool.request("missing", "init", {"extend": encoded({"loader": str(source)})})
        assert raised.value.code == "plugin_init_failed"
        assert raised.value.details["missing_module"] == "absent_t4_fixture_module"
        assert raised.value.details["root_error"] == "ModuleNotFoundError"
    event = next(item for item in store.events() if item["stage"] == "worker.failed")
    assert event["missing_module"] == "absent_t4_fixture_module"
    assert "error_line" in event
    assert str(tmp_path) not in json.dumps(store.events())


PLUGIN = """
from __future__ import annotations
from dataclasses import dataclass
from base.spider import Spider as Base
import sys
import json

@dataclass
class Record:
    value: str

class Spider(Base):
    def init(self, extend): return Record("ok").value
    def getName(self): return "Registered"
    def homeContent(self, filter):
        return {"registered": __name__ in sys.modules, "module": __name__, "list": []}
    def categoryContent(self, tid, pg, filter, extend):
        FILTER
"""


@pytest.mark.parametrize("wrapped", [False, True])
def test_dataclass_main_and_inner_modules_are_registered_and_cleaned(tmp_path, wrapped):
    source = tmp_path / "main.py"
    source.write_text(PLUGIN.replace("FILTER", "return {'list': []}"), encoding="utf-8")
    runtime = AtvpRuntime()
    try:
        payload = {"source": str(source), "raw": True} if wrapped else {"loader": str(source)}
        assert runtime.init(encoded(payload))["result"] == "ok"
        home = runtime.home()
        assert home["registered"] is True
        name = home["module"]
        assert name in sys.modules
    finally:
        runtime.close()
    assert name not in sys.modules


@pytest.mark.parametrize("body", [
    "return {'value': extend.get('sort', 'empty')}",
    "return {'value': json.loads(extend).get('sort', 'empty')}",
])
@pytest.mark.parametrize("extend", [{"sort": "new"}, '{"sort":"new"}'])
def test_category_contract_is_chosen_before_invocation(tmp_path, body, extend):
    source = tmp_path / "main.py"
    source.write_text(PLUGIN.replace("FILTER", body), encoding="utf-8")
    with AtvpRuntimeContext() as runtime:
        runtime.init(encoded({"source": str(source), "raw": True}))
        assert runtime.category("all", "1", True, extend)["value"] == "new"


class AtvpRuntimeContext(AtvpRuntime):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def test_dataclass_filter_is_registered_without_changing_vendor(tmp_path):
    source = tmp_path / "main.py"
    source.write_text(PLUGIN.replace("FILTER", "return {'list': []}"), encoding="utf-8")
    filtered = tmp_path / "filter.py"
    filtered.write_text("""
from __future__ import annotations
from dataclasses import dataclass
@dataclass
class State:
    value: str
class Filter:
    def home(self, value, context):
        value["filter_loaded"] = State("yes").value
        return value
""", encoding="utf-8")
    with AtvpRuntimeContext() as runtime:
        runtime.init(encoded({
            "source": str(source), "raw": True,
            "filters": [{"source": str(filtered), "stages": ["home"]}],
        }))
        # ATVP does not run home filters; inspect the actual loaded filter instance.
        filters = runtime._spider._filters
        assert len(filters) == 1
        assert filters[0]["instance"].home({}, {})["filter_loaded"] == "yes"


def test_exception_metadata_ignores_unsafe_names_and_cyclic_causes():
    error = ModuleNotFoundError("private", name="https://private.example/token")
    error.__cause__ = error
    assert error_details(error) == {"root_error": "ModuleNotFoundError"}


def test_nested_json_filter_value_does_not_change_dictionary_contract(tmp_path):
    source = tmp_path / "main.py"
    source.write_text(PLUGIN.replace(
        "FILTER", "return {'value': json.loads(extend.get('options', '{}')).get('sort')}",
    ), encoding="utf-8")
    with AtvpRuntimeContext() as runtime:
        runtime.init(encoded({"source": str(source), "raw": True}))
        assert runtime.category("all", "1", True, {"options": '{"sort":"new"}'})["value"] == "new"


def test_raw_plugin_keeps_standard_module_type_behavior(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("""
import types
class Spider:
    def init(self, extend): pass
    def getName(self): return "Raw"
    def homeContent(self, filter): return {"name": types.ModuleType("expected").__name__}
""", encoding="utf-8")
    with AtvpRuntimeContext() as runtime:
        runtime.init(encoded({"loader": str(source)}))
        assert runtime.home()["name"] == "expected"


def test_existing_common_directory_is_discovered_without_new_setting(tmp_path, monkeypatch):
    common = tmp_path / "common"
    common.mkdir()
    (common / "t4_shared_fixture.py").write_text("title = 'Shared'", encoding="utf-8")
    monkeypatch.setenv("T4_PLUGIN_DIR", str(tmp_path))
    source = tmp_path / "main.py"
    source.write_text("""
from t4_shared_fixture import title
class Spider:
    def init(self, extend): pass
    def getName(self): return title
""", encoding="utf-8")
    try:
        with AtvpRuntimeContext() as runtime:
            assert runtime.init(encoded({"loader": str(source)}))["name"] == "Shared"
        assert str(common.resolve()) not in sys.path
    finally:
        sys.modules.pop("t4_shared_fixture", None)
