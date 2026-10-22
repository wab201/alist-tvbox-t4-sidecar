from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


INDEX_PATH = Path(__file__).parents[2] / "web" / "index.html"


def build_web_router() -> APIRouter:
    router = APIRouter()
    page = INDEX_PATH.read_text(encoding="utf-8")

    @router.get("/t4/", response_class=HTMLResponse)
    async def t4_web() -> HTMLResponse:
        return HTMLResponse(page)

    return router
