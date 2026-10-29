"""Run the read-only subscription sample gate; exit 2 when the gate is not passed."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path

from sidecar.compatibility.runner import CheckOptions
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app


async def run(args) -> dict:
    settings = replace(Settings.from_env(), compatibility_report=str(args.report))
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runner = app.state.compatibility
        runner.start(CheckOptions(
            source_ids=args.source, plugin_ids=args.plugin, keyword=args.keyword,
            concurrency=args.concurrency, category_limit=args.categories,
            line_limit=args.lines, navigation_depth=args.navigation_depth,
        ), "http://127.0.0.1:8765")
        await runner.task
        return runner.report


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
    arguments = parser.parse_args()
    try:
        report = asyncio.run(run(arguments))
        print(json.dumps({key: report[key] for key in (
            "state", "progress", "summary", "stage_summary", "gate",
        )}, ensure_ascii=True))
        raise SystemExit(0 if report["gate"]["passed"] else 2)
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": type(exc).__name__}))
        raise SystemExit(1)
