from fastapi.testclient import TestClient

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry


PYTHON_PLUGIN = b"""
from base.spider import Spider as BaseSpider


class Spider(BaseSpider):
    def init(self, extend):
        return "upload-init-ok"

    def getName(self):
        return "Uploaded Python"

    def homeContent(self, filter):
        return {"class": [], "list": [{"vod_id": "upload-1"}]}

    def categoryContent(self, tid, pg, filter, extend):
        return {"page": int(pg), "list": [{"vod_id": "upload-2"}]}

    def detailContent(self, ids):
        return {"list": [{
            "vod_id": ids[0],
            "vod_play_from": "Uploaded",
            "vod_play_url": "Play$upload-play",
        }]}

    def searchContent(self, key, quick, pg):
        return {"page": int(pg), "list": [{"vod_id": "upload-3"}]}

    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "url": "https://video.invalid/upload.m3u8"}

    def action(self, action):
        return {"action": action, "list": []}
"""


JAVASCRIPT_PLUGIN = b"""
module.exports = {
  init: () => "upload-js-init-ok",
  getName: () => "Uploaded JavaScript",
  homeContent: () => ({class: [], list: [{vod_id: "js-1"}]}),
  categoryContent: (tid, pg) => ({page: Number(pg), list: [{vod_id: "js-2"}]}),
  detailContent: (ids) => ({
    list: [{vod_id: ids[0], vod_play_from: "Uploaded", vod_play_url: "Play$js-play"}]
  }),
  searchContent: (key, quick, pg) => ({page: Number(pg), list: [{vod_id: "js-3"}]}),
  playerContent: () => ({parse: 0, url: "https://video.invalid/js.m3u8"}),
  action: (action) => ({action, list: []}),
};
"""


def test_python_upload_is_registered_as_local_source(tmp_path) -> None:
    app = create_app(
        Settings(plugin_dir=str(tmp_path)),
        SourceRegistry(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/t4/api/plugins/upload",
            files={"file": ("demo.py", PYTHON_PLUGIN, "text/x-python")},
        )

        assert response.status_code == 200
        plugin = response.json()["plugin"]
        assert plugin["type"] == "raw_python"
        assert plugin["fm_path"].startswith("/t4/fm/local-uploaded/")

        sources = client.get("/t4/api/sources").json()["sources"]
        assert sources[0]["id"] == "local-uploaded"

        home = client.get(
            "/t4/api/home",
            params={"source": "local-uploaded", "site": plugin["id"]},
        )
        assert home.status_code == 200
        assert home.json()["list"][0]["vod_id"].startswith("t4_")

        category = client.get(
            "/t4/api/category",
            params={
                "source": "local-uploaded",
                "site": plugin["id"],
                "t": "all",
                "pg": "2",
            },
        )
        assert category.status_code == 200
        assert category.json()["page"] == 2

        search = client.get(
            "/t4/api/search",
            params={
                "source": "local-uploaded",
                "site": plugin["id"],
                "wd": "demo",
                "pg": "3",
            },
        )
        assert search.status_code == 200
        assert search.json()["page"] == 3

        detail = client.get(
            "/t4/api/detail",
            params={
                "source": "local-uploaded",
                "site": plugin["id"],
                "ids": home.json()["list"][0]["vod_id"],
            },
        )
        assert detail.status_code == 200
        play_id = detail.json()["list"][0]["vod_play_url"].split("$", 1)[1]

        play = client.get(
            "/t4/api/play",
            params={
                "source": "local-uploaded",
                "site": plugin["id"],
                "flag": "Uploaded",
                "play": play_id,
            },
        )
        assert play.status_code == 200
        target = play.json()["url"]
        assert target.startswith("http://testserver/t4/media/")
        assert client.app.state.playback.get(target.rsplit("/", 1)[-1]).url.endswith("upload.m3u8")

        action = client.post(
            "/t4/api/action",
            json={
                "source": "local-uploaded",
                "site": plugin["id"],
                "action": "refresh",
            },
        )
        assert action.status_code == 200
        assert action.json()["action"] == "refresh"

        reloaded = create_app(
            Settings(plugin_dir=str(tmp_path)),
            SourceRegistry(),
        )
        with TestClient(reloaded) as reloaded_client:
            snapshot = reloaded_client.get(
                "/t4/api/sources/local-uploaded/subscription"
            )
            assert snapshot.status_code == 200
            assert snapshot.json()["plugins"][0]["id"] == plugin["id"]

        deleted = client.delete(f"/t4/api/plugins/{plugin['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert client.get("/t4/api/sources").json()["sources"] == []


def test_javascript_upload_is_registered_and_executable(tmp_path) -> None:
    app = create_app(
        Settings(plugin_dir=str(tmp_path)),
        SourceRegistry(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/t4/api/plugins/upload",
            files={"file": ("demo.js", JAVASCRIPT_PLUGIN, "text/javascript")},
        )

        assert response.status_code == 200
        plugin = response.json()["plugin"]
        assert plugin["type"] == "javascript"

        home = client.get(
            "/t4/api/home",
            params={"source": "local-uploaded", "site": plugin["id"]},
        )
        assert home.status_code == 200
        assert home.json()["list"][0]["vod_id"].startswith("t4_")

        detail = client.get(
            "/t4/api/detail",
            params={
                "source": "local-uploaded",
                "site": plugin["id"],
                "ids": home.json()["list"][0]["vod_id"],
            },
        )
        assert detail.status_code == 200
        play_id = detail.json()["list"][0]["vod_play_url"].split("$", 1)[1]

        for path, params in (
            (
                "/t4/api/category",
                {"t": "all", "pg": "2"},
            ),
            (
                "/t4/api/search",
                {"wd": "demo", "pg": "3"},
            ),
        ):
            response = client.get(
                path,
                params={
                    "source": "local-uploaded",
                    "site": plugin["id"],
                    **params,
                },
            )
            assert response.status_code == 200

        play = client.get(
            "/t4/api/play",
            params={
                "source": "local-uploaded",
                "site": plugin["id"],
                "flag": "Uploaded",
                "play": play_id,
            },
        )
        assert play.status_code == 200
        target = play.json()["url"]
        assert target.startswith("http://testserver/t4/media/")
        assert client.app.state.playback.get(target.rsplit("/", 1)[-1]).url.endswith("js.m3u8")

        action = client.post(
            "/t4/api/action",
            json={
                "source": "local-uploaded",
                "site": plugin["id"],
                "action": "refresh",
            },
        )
        assert action.status_code == 200
        assert action.json()["action"] == "refresh"
