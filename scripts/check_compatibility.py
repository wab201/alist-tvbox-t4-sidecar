"""Check every subscription plugin by default; exit 2 if the sample gate fails."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import tempfile

import uvicorn

from sidecar.compatibility.runner import CheckOptions
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app


async def run(args) -> dict:
    settings = replace(Settings.from_env(), compatibility_report=str(args.report))
    with tempfile.TemporaryDirectory(prefix="t4-compatibility-") as directory:
        if args.isolated:
            settings = replace(
                settings, plugin_dir=directory, source_config_file=None, log_file=None,
            )
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            # Probe the candidate's own image/media routes, never production port 8765.
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                base = f"http://127.0.0.1:{listener.getsockname()[1]}"
                server = uvicorn.Server(uvicorn.Config(
                    app, lifespan="off", access_log=False, log_level="error",
                ))
                serving = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    while not server.started:
                        if serving.done():
                            await serving
                            raise RuntimeError("probe_server_not_started")
                        await asyncio.sleep(.01)
                    runner = app.state.compatibility
                    runner.start(CheckOptions(
                        source_ids=args.source, plugin_ids=args.plugin, keyword=args.keyword,
                        concurrency=args.concurrency, category_limit=args.categories,
                        line_limit=args.lines, navigation_depth=args.navigation_depth,
                    ), base)
                    previous = None
                    while not runner.task.done():
                        progress = runner.report["progress"]
                        if args.progress and progress != previous:
                            print(json.dumps({
                                "state": runner.report["state"], "progress": progress,
                                "summary": runner.report.get("summary", {}),
                            }), flush=True)
                            previous = dict(progress)
                        await asyncio.wait({runner.task}, timeout=1)
                    await runner.task
                    return runner.report
                finally:
                    server.should_exit = True
                    await serving


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=Path("tests/reports/compatibility-latest.json"))
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--plugin", action="append", default=[],
                        help="Repeat to check only these plugin IDs.")
    parser.add_argument("--keyword", default="")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--categories", type=int, default=2)
    parser.add_argument("--lines", type=int, default=4)
    parser.add_argument("--navigation-depth", type=int, default=8)
    parser.add_argument("--isolated", action="store_true",
                        help="Use the environment subscription and temporary plugin storage.")
    parser.add_argument("--progress", action="store_true",
                        help="Print redacted incremental counts.")
    arguments = parser.parse_args()
    try:
        report = asyncio.run(run(arguments))
        print(json.dumps({key: report[key] for key in (
            "state", "progress", "summary", "stage_summary", "coverage", "gate",
        )}, ensure_ascii=True))
        raise SystemExit(0 if report["gate"]["passed"] else 2)
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": type(exc).__name__}))
        raise SystemExit(1)
