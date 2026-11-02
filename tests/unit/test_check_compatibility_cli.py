import asyncio
from types import SimpleNamespace

import httpx

from scripts.check_compatibility import run
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceRegistry


def test_cli_probes_its_own_http_server_and_isolates_storage(tmp_path, monkeypatch):
    seen = {}

    class Runner:
        report = {"state": "completed", "progress": {"total": 0, "completed": 0}}

        def start(self, options, base):
            async def check():
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.get(base + "/t4/health")
                    assert response.status_code == 200
                seen["base"] = base
            self.task = asyncio.create_task(check())

        async def close(self):
            await self.task

    def factory(settings):
        seen["settings"] = settings
        app = create_app(settings, SourceRegistry())
        app.state.compatibility = Runner()
        return app

    monkeypatch.setenv("T4_PLUGIN_DIR", str(tmp_path / "production"))
    monkeypatch.setenv("T4_SOURCES_FILE", str(tmp_path / "production.json"))
    monkeypatch.setenv("T4_LOG_FILE", str(tmp_path / "production.log"))
    monkeypatch.setattr("scripts.check_compatibility.create_app", factory)
    args = SimpleNamespace(
        report=tmp_path / "report.json", isolated=True, source=[], plugin=[],
        keyword="", concurrency=2, categories=1, lines=1, navigation_depth=4,
        progress=True,
    )
    result = asyncio.run(run(args))
    assert result["state"] == "completed"
    assert seen["base"].startswith("http://127.0.0.1:")
    assert seen["settings"].source_config_file is None
    assert seen["settings"].log_file is None
    assert seen["settings"].plugin_dir != str(tmp_path / "production")
    assert not (tmp_path / "production").exists()
