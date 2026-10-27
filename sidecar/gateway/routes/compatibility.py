from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from sidecar.compatibility.runner import CheckOptions, CompatibilityRunner, artifact_identity


def build_compatibility_router(runner: CompatibilityRunner) -> APIRouter:
    router = APIRouter(prefix="/t4/api/compatibility")

    def latest():
        report = runner.report
        if report is not None:
            # Historic evidence cannot certify a changed workspace or image.
            current = report["artifact"]["tree_sha256"] == artifact_identity()["tree_sha256"]
            report["artifact_current"] = current
            if not current:
                report["gate"].update(passed=False, reason="artifact_changed")
            if runner.running and report["state"] == "completed":
                return {**report, "state": "running",
                        "gate": {**report["gate"], "passed": False, "reason": "report_pending"}}
        return report

    @router.get("/latest")
    async def get_latest():
        return {"report": latest()}

    @router.post("/runs", status_code=202)
    async def start(options: CheckOptions, request: Request):
        try:
            return runner.start(options, str(request.base_url))
        except RuntimeError as exc:
            raise HTTPException(409, detail={"message": "已有兼容检测任务正在运行"}) from exc
        except ValueError as exc:
            raise HTTPException(422, detail={"message": "订阅不存在"}) from exc

    @router.post("/cancel", status_code=202)
    async def cancel():
        runner.cancel()
        return {"state": runner.report["state"] if runner.report else "idle"}

    @router.get("/export")
    async def export():
        report = latest()
        if report is None:
            raise HTTPException(404, detail={"message": "暂无检测报告"})
        return Response(
            json.dumps(report, ensure_ascii=True, indent=2) + "\n",
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="t4-compatibility.json"',
                     "Cache-Control": "no-store"},
        )

    return router
