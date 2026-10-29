import base64
import json

import pytest
from fastapi.testclient import TestClient

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.worker.atvp_runtime import AtvpRuntime
from tests.worker.test_secspider_runtime import build_secspider_fixture


PYTHON_SOURCE = """
from base.spider import Spider as Base
import json
class Spider(Base):
    def init(self, extend): pass
    def getName(self): return "JSON fixture"
    def homeContent(self, filter):
        return json.dumps({"class": [{"type_id": "movies", "type_name": "Movies"}],
                           "list": [{"vod_id": "item", "vod_name": "Fixture"}]})
    def categoryContent(self, tid, pg, filter, extend):
        return json.dumps({"page": int(pg), "list": [{"vod_id": "item", "vod_name": extend["sort"]}]})
    def searchContent(self, key, quick, pg="1"):
        return json.dumps({"page": int(pg), "list": [{"vod_id": "item", "vod_name": key}]})
    def detailContent(self, ids):
        return json.dumps({"list": [{"vod_id": ids[0], "vod_name": "Fixture",
            "vod_pic": "https://media.example/cover.jpg",
            "vod_play_from": "HD", "vod_play_url": "Episode$episode"}]})
    def playerContent(self, flag, id, vipFlags):
        return json.dumps({"parse": 0, "url": "https://media.example/" + id + ".mkv",
                           "header": {"Referer": "https://drive.example"}})
"""


JS_SOURCE = """
const plugin = {
  init() {},
  getName() { return "JSON fixture"; },
  homeContent() {
    return JSON.stringify({class: [{type_id: "movies", type_name: "Movies"}],
                           list: [{vod_id: "item", vod_name: "Fixture"}]});
  },
  categoryContent(tid, pg, filter, extend) {
    return JSON.stringify({page: Number(pg), list: [{vod_id: "item", vod_name: extend.sort}]});
  },
  searchContent(key, quick, pg) {
    return JSON.stringify({page: Number(pg), list: [{vod_id: "item", vod_name: key}]});
  },
  detailContent(ids) {
    return JSON.stringify({list: [{vod_id: ids[0], vod_name: "Fixture",
      vod_pic: "https://media.example/cover.jpg",
      vod_play_from: "HD", vod_play_url: "Episode$episode"}]});
  },
  playerContent(flag, id) {
    return JSON.stringify({parse: 0, url: "https://media.example/" + id + ".mkv",
                           header: {Referer: "https://drive.example"}});
  }
};
export default plugin;
"""


@pytest.mark.parametrize("suffix,source", [("py", PYTHON_SOURCE), ("js", JS_SOURCE)], ids=["python", "javascript"])
@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_uploaded_json_string_results_work_across_real_worker_and_http(tmp_path, suffix, source, transport):
    app = create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())
    with TestClient(app) as client:
        uploaded = client.post("/t4/api/plugins/upload", files={
            "file": ("fixture." + suffix, source.encode()),
        })
        assert uploaded.status_code == 200
        api = uploaded.json()["plugin"]["fm_path"]

        def call(params):
            if transport == "GET":
                return client.get(api, params=params)
            if transport == "JSON":
                return client.post(api, json=params)
            return client.post(api, data=params)

        home = call({})
        assert home.status_code == 200, home.text
        assert home.json()["class"][0]["type_id"] == "movies"
        category = call({"t": "movies", "pg": 2, "ext": json.dumps({"sort": "new"})})
        assert category.status_code == 200
        assert category.json()["page"] == 2
        assert category.json()["list"][0]["vod_name"] == "new"
        search = call({"wd": "Fixture", "pg": 3, "quick": False})
        assert search.status_code == 200 and search.json()["page"] == 3
        detail = call({"ac": "detail", "ids": home.json()["list"][0]["vod_id"]})
        assert detail.status_code == 200
        vod = detail.json()["list"][0]
        assert vod["vod_pic"].endswith(".jpg")
        play_id = vod["vod_play_url"].partition("$")[2]
        assert play_id.startswith("t4_")
        play = call({"play": play_id, "flag": "HD"})
        assert play.status_code == 200, play.text
        assert play.json()["url"] == "https://media.example/episode.mkv"
        assert play.json()["header"] == {"Referer": "https://drive.example"}


@pytest.mark.parametrize("encrypted", [False, True], ids=["raw", "secspider"])
def test_json_results_are_decoded_before_atvp_category_and_filter_processing(tmp_path, encrypted):
    source = tmp_path / "wrapped.py"
    source.write_text(PYTHON_SOURCE.replace(
        "class Spider(Base):", "class Spider(Base):\n    backend_parse = True",
    ), encoding="utf-8")
    filter_path = tmp_path / "filter.py"
    filter_path.write_text("""
class Filter:
    def detail(self, value, context):
        value["list"][0]["vod_remarks"] = "filtered"
        return value
""", encoding="utf-8")
    payload = {
        "source": str(source), "raw": True,
        "filters": [{"source": str(filter_path), "stages": ["detail"]}],
    }
    if encrypted:
        package, loader, _, _ = build_secspider_fixture(tmp_path, source.read_text(encoding="utf-8"))
        payload.update(source=package, loader=loader, raw=False, secspider_loader="self")
    extend = base64.b64encode(json.dumps(payload).encode()).decode()
    runtime = AtvpRuntime()
    try:
        runtime.init(extend)
        home = runtime.home()
        assert home["list"][0]["vod_id"] == "atvp_detail:item"
        assert home["list"][0]["vod_tag"] == "folder"
        search = runtime.search("Fixture", False, "2")
        assert search["list"][0]["vod_id"] == "atvp_detail:item"
        category = runtime.category("movies", "2", True, {"sort": "new"})
        assert category["list"][0]["vod_id"] == "atvp_detail:item"
        detail = runtime.detail(["item"])
        assert detail["list"][0]["vod_remarks"] == "filtered"
        assert detail["list"][0]["vod_play_url"] == "Episode$episode"
    finally:
        runtime.close()


@pytest.mark.parametrize("value", [
    "not JSON", '"nested string"', "null", "[]", "true", "123",
])
def test_invalid_non_object_results_are_not_replaced_with_empty_success(tmp_path, value):
    source = PYTHON_SOURCE.replace(
        'return json.dumps({"class": [{"type_id": "movies", "type_name": "Movies"}],\n'
        '                           "list": [{"vod_id": "item", "vod_name": "Fixture"}]})',
        "return " + repr(value),
    )
    with TestClient(create_app(Settings(plugin_dir=str(tmp_path)), SourceRegistry())) as client:
        uploaded = client.post("/t4/api/plugins/upload", files={"file": ("invalid.py", source.encode())})
        api = uploaded.json()["plugin"]["fm_path"]
        response = client.get(api)
        assert response.status_code == 400
        assert response.json()["detail"]["message"] == "home result must be an object"


def test_init_name_and_action_strings_are_not_interpreted_as_json(tmp_path):
    source = tmp_path / "literal.py"
    source.write_text("""
class Spider:
    def init(self, extend): return "null"
    def getName(self): return "123"
    def action(self, action): return "true"
""", encoding="utf-8")
    runtime = AtvpRuntime()
    try:
        extend = base64.b64encode(json.dumps({"loader": str(source)}).encode()).decode()
        assert runtime.init(extend) == {"result": "null", "name": "123"}
        assert runtime.action("message") == "true"
    finally:
        runtime.close()
