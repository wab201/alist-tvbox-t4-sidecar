from fastapi import APIRouter, HTTPException, Request, Response
import httpx

from sidecar.diagnostics import record
from sidecar.images import ImageBridge


def build_images_router(images: ImageBridge) -> APIRouter:
    router = APIRouter()

    @router.api_route("/t4/images/{key}", methods=["GET", "HEAD"])
    async def image(key: str, request: Request):
        try:
            content, mime = await images.fetch(key)
        except KeyError as exc:
            raise HTTPException(410, "图片地址已过期，请刷新列表") from exc
        except (httpx.HTTPError, ValueError) as exc:
            record("image.failed", error=type(exc).__name__)
            raise HTTPException(502, "图片上游不可用") from exc
        return Response(
            b"" if request.method == "HEAD" else content, media_type=mime,
            headers={"Content-Length": str(len(content)), "Cache-Control": "private, max-age=300"},
        )

    return router
