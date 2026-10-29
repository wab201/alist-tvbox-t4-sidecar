import base64
from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.worker.atvp_runtime import AtvpRuntime, AtvpRuntimeError
from tests.worker.test_secspider_runtime import build_secspider_fixture, VENDOR_ATVP


SOURCE = """
from base.spider import Spider as Base
class Spider(Base):
    backend_parse = True
    def init(self, extend): pass
    def getName(self): return "Cloud fixture"
    def homeContent(self, filter):
        return {"class": [{"type_id": "all", "type_name": "Movies"}]}
    def categoryContent(self, tid, pg, filter, extend):
        return {"list": [{"vod_id": "https://catalog.example/movie/1", "vod_name": "Movie"}]}
    def searchContent(self, key, quick, pg="1"):
        return self.categoryContent("all", pg, True, {})
    def detailContent(self, ids):
        assert ids == ["https://catalog.example/movie/1"], "wrong inner detail ID"
        return {"list": [{
            "vod_id": ids[0], "vod_name": "Movie", "vod_pic": "https://image.example/1.jpg",
            "vod_actor": "Actor", "vod_remarks": "4K",
            "vod_play_from": "Drive A$$$Drive B",
            "vod_play_url": "Share A$https://share.example/a$$$Share B$push://https://share.example/b",
            "group": [{"name": "Cloud", "media": [
                {"name": "Share B", "url": "push://https://share.example/b"}]}],
        }]}
"""


@contextmanager
def backend(directory, *, failed=()):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, value, status=200):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/parse/fixture?ac=play"
            share = body["url"]
            calls.append(share)
            if share in failed:
                return self.reply({}, 502)
            drive = share.rsplit("/", 1)[-1]
            self.reply({"list": [{
                "vod_id": drive, "vod_play_from": "Original",
                "vod_play_url": "Episode 1$1@" + drive + "-1#Episode 2$1@" + drive + "-2",
            }]})

        def do_GET(self):
            files = {
                "/cloud.py": directory / "cloud.py",
                "/encrypted-demo.txt": directory / "encrypted-demo.txt",
                "/Atvp.py": (
                    directory / "Atvp-custom.py"
                    if (directory / "Atvp-custom.py").exists() else VENDOR_ATVP
                ),
            }
            if self.path in files:
                data = files[self.path].read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            assert self.path.startswith("/play/fixture?")
            self.reply({"url": "https://media.example/video.mkv",
                        "parse": 0, "header": {"Referer": "https://drive.example/"}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def payload_for(tmp_path, encrypted, base, source=SOURCE):
    if encrypted:
        path, loader, _, _ = build_secspider_fixture(tmp_path, source)
        payload = {"source": path, "loader": loader, "raw": False, "secspider_loader": "self"}
    else:
        path = tmp_path / "cloud.py"
        path.write_text(source, encoding="utf-8")
        payload = {"source": str(path), "raw": True}
    if base:
        payload.update(source=base + "/" + str(path).replace("\\", "/").rsplit("/", 1)[-1],
                       loader=base + "/Atvp.py")
    return {**payload, "api": base, "token": "fixture"}


@pytest.mark.parametrize("encrypted", [False, True], ids=["raw", "secspider"])
@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
@pytest.mark.parametrize("listing", ["category", "search"])
def test_prefixed_cloud_detail_expands_all_lines_through_real_worker_and_http(
    tmp_path, encrypted, transport, listing,
):
    with backend(tmp_path) as (base, calls):
        payload = payload_for(tmp_path, encrypted, base)
        async def subscription(_):
            return {"sites": [{"key": "cloud", "api": "csp_PyProxy", "ext": payload}]}
        registry = SourceRegistry((SourceConfig("at", "AT", subscription_url=base + "/sub/fixture/1"),))
        app = create_app(Settings(plugin_dir=str(tmp_path / "plugins")), registry, subscription_fetcher=subscription)
        with TestClient(app) as client:
            def call(params):
                api = "/t4/fm/at/cloud/"
                if transport == "GET":
                    return client.get(api, params=params)
                if transport == "JSON":
                    return client.post(api, json=params)
                return client.post(api, data=params)
            cards = call({"t": "all"} if listing == "category" else {"wd": "Movie"})
            assert cards.status_code == 200, cards.text
            card = cards.json()["list"][0]
            assert card["vod_tag"] == "folder"
            # Directory-capable clients retain the original ATVP category flow.
            folder = call({"t": card["vod_id"], "pg": 1})
            assert len(folder.json()["list"]) == 2
            detail = call({"ids": card["vod_id"], "ac": "detail"})
            assert detail.status_code == 200, detail.text
            vod = detail.json()["list"][0]
            assert vod["vod_name"] == "Movie"
            assert vod["vod_actor"] == "Actor"
            assert vod["vod_pic"] == "https://image.example/1.jpg"
            assert vod["vod_play_from"] == "Drive A$$$Drive B"
            assert vod["vod_tag"] == "file" and "group" not in vod
            for flag, group in zip(vod["vod_play_from"].split("$$$"), vod["vod_play_url"].split("$$$")):
                assert len(group.split("#")) == 2
                for episode in group.split("#"):
                    play = call({"play": episode.partition("$")[2], "flag": flag})
                    assert play.status_code == 200, play.text
                    assert play.json()["url"] == "https://media.example/video.mkv"
                    assert play.json()["header"] == {"Referer": "https://drive.example/"}
        assert sorted(calls) == ["https://share.example/a", "https://share.example/b"]


def test_dynamic_atvp_group_detail_selects_the_original_group(tmp_path):
    with backend(tmp_path) as (base, calls):
        payload = payload_for(tmp_path, False, base)
        loader = tmp_path / "group-loader.py"
        loader.write_text(VENDOR_ATVP.read_text(encoding="utf-8") + """
Spider._decode_group_id = lambda self, value: (
    ("https://catalog.example/movie/1", 0) if value == "atvp_group:fixture" else None
)
""", encoding="utf-8")
        payload["loader"] = str(loader)
        runtime = AtvpRuntime()
        try:
            runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
            detail = runtime.detail(["atvp_group:fixture"])
            assert detail["list"][0]["vod_play_from"] == "Cloud"
            assert detail["list"][0]["vod_play_url"] == "Episode 1$1@b-1#Episode 2$1@b-2"
            assert calls == ["https://share.example/b"]
        finally:
            runtime.close()


def test_group_only_detail_and_duplicate_names_keep_all_resources(tmp_path):
    source = SOURCE.replace(
        '"Share A$https://share.example/a$$$Share B$push://https://share.example/b"', '""',
    ).replace(
        '{"name": "Share B", "url": "push://https://share.example/b"}',
        '{"name": "Share A", "url": "https://share.example/a"},'
        '{"name": "Share B", "url": "push://https://share.example/b"}',
    )
    with backend(tmp_path) as (base, calls):
        runtime = AtvpRuntime()
        try:
            payload = payload_for(tmp_path, False, base, source)
            runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
            detail = runtime.detail(["atvp_detail:https://catalog.example/movie/1"])
            assert detail["list"][0]["vod_play_from"] == "Cloud$$$Cloud (2)"
            assert len(detail["list"][0]["vod_play_url"].split("$$$")) == 2
            assert sorted(calls) == ["https://share.example/a", "https://share.example/b"]
        finally:
            runtime.close()


def test_http_prefetch_is_concurrent_while_filters_stay_on_the_worker_thread(tmp_path):
    source = SOURCE.replace(
        '"Share A$https://share.example/a$$$Share B$push://https://share.example/b"',
        '"A$https://share.example/a$$$B$https://share.example/b$$$Again$https://share.example/a"',
    ).replace('"Drive A$$$Drive B"', '"Drive A$$$Drive B$$$Again"')
    payload = payload_for(tmp_path, False, "https://at.example", source)
    # This unit check uses the exact loader and isolates only its HTTP boundary.
    payload.update(source=str(tmp_path / "cloud.py"), loader="")
    runtime = AtvpRuntime()
    responses_ready = threading.Barrier(2)
    main_thread = threading.get_ident()
    calls = []
    try:
        runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
        spider = runtime._spider
        def post(url, **kwargs):
            target = kwargs["json"]["url"]
            assert kwargs["params"] == {"ac": "play"}
            assert kwargs["json"]["title"] == "Movie"
            calls.append(target)
            responses_ready.wait(3)
            response = {"list": [{"vod_play_from": "Drive", "vod_play_url": "Episode$1@fixture"}]}
            return SimpleNamespace(status_code=200, text=json.dumps(response))
        original_filters = spider._run_filters
        def filters(*args, **kwargs):
            assert threading.get_ident() == main_thread
            return original_filters(*args, **kwargs)
        spider.post = post
        spider._run_filters = filters
        result = runtime.detail(["atvp_detail:https://catalog.example/movie/1"])
        assert result["list"][0]["vod_play_from"] == "Drive A$$$Drive B$$$Again"
        assert sorted(calls) == ["https://share.example/a", "https://share.example/b"]
        assert spider.post is post
    finally:
        runtime.close()


@pytest.mark.parametrize("all_failed", [False, True])
def test_failed_share_does_not_hide_other_lines_or_return_empty_success(tmp_path, all_failed):
    failed = ["https://share.example/a"]
    if all_failed:
        failed.append("https://share.example/b")
    with backend(tmp_path, failed=failed) as (base, _):
        runtime = AtvpRuntime()
        try:
            payload = payload_for(tmp_path, False, base)
            runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
            if all_failed:
                with pytest.raises(AtvpRuntimeError, match="no available episodes"):
                    runtime.detail(["atvp_detail:https://catalog.example/movie/1"])
            else:
                detail = runtime.detail(["atvp_detail:https://catalog.example/movie/1"])
                assert detail["list"][0]["vod_play_from"] == "Drive B"
                assert detail["t4_warnings"] == [{
                    "code": "cloud_detail_unavailable", "line": 1, "error": "AtvpRuntimeError",
                    "http_status": 502,
                }]
        finally:
            runtime.close()


def test_prefix_bridge_preserves_direct_episode_lists_and_multi_id_contract(tmp_path):
    source = SOURCE.replace(
        '"Share A$https://share.example/a$$$Share B$push://https://share.example/b"',
        '"Episode$https://media.example/direct.m3u8$$$Episode 1$episode-1#Episode 2$episode-2"',
    )
    payload = payload_for(tmp_path, False, "", source)
    runtime = AtvpRuntime()
    try:
        runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
        result = runtime.detail([
            "atvp_detail:https://catalog.example/movie/1",
            "atvp_detail:https://catalog.example/movie/1",
        ])
        assert len(result["list"]) == 2
        assert result["list"][0]["vod_play_url"] == (
            "Episode$https://media.example/direct.m3u8$$$Episode 1$episode-1#Episode 2$episode-2"
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("encrypted", [False, True], ids=["raw", "secspider"])
@pytest.mark.parametrize("partial", [False, True])
def test_multiple_shares_inside_one_line_expand_without_losing_direct_episodes(tmp_path, encrypted, partial):
    source = SOURCE.replace(
        '"Share A$https://share.example/a$$$Share B$push://https://share.example/b"',
        '"Direct$https://media.example/direct.m3u8#Share A$https://share.example/a'
        '#Share B$push://https://share.example/b#Last$episode-last"',
    ).replace('"Drive A$$$Drive B"', '"Drive"')
    with backend(tmp_path, failed=["https://share.example/a"] if partial else []) as (base, calls):
        runtime = AtvpRuntime()
        try:
            payload = payload_for(tmp_path, encrypted, base, source)
            runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
            result = runtime.detail(["atvp_detail:https://catalog.example/movie/1"])
            vod = result["list"][0]
            assert vod["vod_play_from"] == (
                "Drive$$$Drive / Share B$$$Drive (2)" if partial else
                "Drive$$$Drive / Share A$$$Drive / Share B$$$Drive (2)"
            )
            groups = vod["vod_play_url"].split("$$$")
            assert groups[0] == "Direct$https://media.example/direct.m3u8"
            assert groups[-1] == "Last$episode-last"
            assert all(len(group.split("#")) == 2 for group in groups[1:-1])
            assert not any("share.example" in group for group in groups)
            assert sorted(calls) == ["https://share.example/a", "https://share.example/b"]
            if partial:
                assert result["t4_warnings"][0]["line"] == 1
                assert result["t4_warnings"][0]["entry"] == 2
        finally:
            runtime.close()


@pytest.mark.parametrize("encrypted", [False, True], ids=["raw", "secspider"])
def test_legacy_two_argument_search_still_returns_wrapped_cloud_cards(tmp_path, encrypted):
    source = SOURCE.replace(
        'def searchContent(self, key, quick, pg="1"):\n        return self.categoryContent("all", pg, True, {})',
        'def searchContent(self, key, quick):\n        return self.categoryContent("all", "1", True, {})',
    )
    with backend(tmp_path) as (base, _):
        runtime = AtvpRuntime()
        try:
            payload = payload_for(tmp_path, encrypted, base, source)
            runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
            result = runtime.search("Movie", False, "2")
            assert result["list"][0]["vod_id"] == "atvp_detail:https://catalog.example/movie/1"
        finally:
            runtime.close()


def test_search_signature_adaptation_does_not_retry_a_plugin_internal_type_error(tmp_path):
    source = SOURCE.replace(
        'return self.categoryContent("all", pg, True, {})',
        'raise TypeError("plugin internal error")',
    )
    runtime = AtvpRuntime()
    try:
        payload = payload_for(tmp_path, False, "", source)
        runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
        with pytest.raises(AtvpRuntimeError, match="TypeError") as caught:
            runtime.search("Movie", False, "2")
        assert isinstance(caught.value.__cause__, TypeError)
    finally:
        runtime.close()


@pytest.mark.parametrize("encrypted", [False, True], ids=["raw", "secspider"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_bare_search_arrays_are_normalized_before_atvp_wraps_the_cards(
    tmp_path, encrypted, as_json, empty,
):
    value = [] if empty else [{"vod_id": "movie-1", "vod_name": "Movie"}]
    expression = repr(json.dumps(value)) if as_json else repr(value)
    source = SOURCE.replace(
        'return self.categoryContent("all", pg, True, {})', "return " + expression,
    )
    runtime = AtvpRuntime()
    try:
        payload = payload_for(tmp_path, encrypted, "", source)
        runtime.init(base64.b64encode(json.dumps(payload).encode()).decode())
        result = runtime.search("Movie", False, "1")
        assert isinstance(result, dict)
        if empty:
            assert result["list"] == []
        else:
            assert result["list"][0]["vod_id"] == "atvp_detail:movie-1"
            assert result["list"][0]["vod_name"] == "Movie"
    finally:
        runtime.close()


def test_search_array_normalization_does_not_change_other_methods_or_invalid_items():
    rows = [{"vod_id": "movie-1", "vod_name": "Movie"}]
    assert AtvpRuntime._plugin_result(rows, method_name="playerContent") == rows
    assert AtvpRuntime._plugin_result(["bad"], method_name="searchContent") == ["bad"]
