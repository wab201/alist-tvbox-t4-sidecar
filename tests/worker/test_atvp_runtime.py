import base64
import json
from pathlib import Path

from sidecar.worker.atvp_runtime import AtvpRuntime
from sidecar.worker.client import WorkerPool


FIXTURE = Path(__file__).parents[1] / "fixtures" / "plugins" / "raw_demo.py"
ATVP = Path(__file__).parents[2] / "sidecar" / "worker" / "vendor" / "Atvp.py"


def make_ext(*, loader: str = "", source: str = str(FIXTURE)) -> str:
    payload = {
        "api": "",
        "source": source,
        "loader": loader,
        "raw": True,
        "token": "-",
        "data": {"fixture": True},
    }
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def test_atvp_runtime_loads_raw_python_and_preserves_method_contract() -> None:
    runtime = AtvpRuntime(str(ATVP))
    try:
        init = runtime.init(make_ext())
        assert init == {"result": "raw-init-ok", "name": "Raw Demo"}
        assert runtime.home()["list"][0]["vod_id"] == "demo-1"
        assert runtime.category("all", "2", True, '{"sort":"new"}')["page"] == 2
        assert runtime.detail(["demo-1"])["list"][0]["vod_play_url"] == "正片$demo-play"
        assert runtime.search("demo", True, "3")["page"] == 3
        assert runtime.play("Demo", "demo-play", [])["url"].endswith("/demo-play.m3u8")
    finally:
        runtime.close()


def test_atvp_runtime_accepts_legacy_http_loader_path(tmp_path) -> None:
    loader = tmp_path / "Atvp.py"
    loader.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    runtime = AtvpRuntime(str(ATVP))
    try:
        payload = {
            "api": "",
            "source": str(FIXTURE),
            "loader": "http://127.0.0.1:4567/Atvp.py",
            "raw": True,
        }
        assert runtime._validate_remote_loader(payload["loader"]) is None
    finally:
        runtime.close()


def test_worker_process_executes_atvp_raw_plugin() -> None:
    with WorkerPool() as pool:
        init = pool.request("source-a", "init", {"extend": make_ext()})
        detail = pool.request("source-a", "detail", {"ids": ["demo-1"]})
        play = pool.request(
            "source-a",
            "play",
            {"flag": "Demo", "id": "demo-play", "vip_flags": []},
        )

    assert init["name"] == "Raw Demo"
    assert detail["list"][0]["vod_name"] == "Demo detail"
    assert play["parse"] == 0
