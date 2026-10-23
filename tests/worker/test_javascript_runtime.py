import base64
import json
import shutil

import pytest

from sidecar.worker.atvp_runtime import AtvpRuntime


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None and shutil.which("nodejs") is None,
    reason="JavaScript runtime is not installed",
)


def test_javascript_runtime_preserves_t4_method_contract(tmp_path) -> None:
    plugin = tmp_path / "demo.js"
    plugin.write_text(
        """
module.exports = {
  init: () => "init-ok",
  getName: () => "JS Demo",
  homeContent: () => ({class: [], list: [{vod_id: "js-1"}]}),
  categoryContent: (tid, pg) => ({page: Number(pg), list: []}),
  detailContent: (ids) => ({list: [{vod_id: ids[0]}]}),
  searchContent: (key, quick, pg) => ({page: Number(pg), list: []}),
  playerContent: () => ({parse: 0, url: "https://video.invalid/js.m3u8"}),
  action: (action) => ({action, list: []}),
};
""",
        encoding="utf-8",
    )
    runtime = AtvpRuntime()
    try:
        extend = base64.b64encode(
            json.dumps(
                {"loader": str(plugin), "raw": True},
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        assert runtime.init(extend)["name"] == "JS Demo"
        assert runtime.home()["list"][0]["vod_id"] == "js-1"
        assert runtime.category("all", "2", True, "")["page"] == 2
        assert runtime.search("demo", True, "3")["page"] == 3
        assert runtime.play("", "id", [])["url"].endswith("js.m3u8")
        assert runtime.action("refresh")["action"] == "refresh"
    finally:
        runtime.close()
