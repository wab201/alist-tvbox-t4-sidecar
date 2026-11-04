from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from sidecar.client_acceleration import ClientAcceleration


class CapabilityProof(BaseModel):
    proof: str = Field(max_length=64)
    protocol: int
    version: str = Field(max_length=16)


def build_acceleration_router(acceleration: ClientAcceleration) -> APIRouter:
    router = APIRouter()

    @router.get("/t4/api/client-acceleration")
    async def status():
        return acceleration.summary()

    @router.get("/t4/acceleration/{identity}/client.jar")
    async def artifact(identity: str):
        content = await asyncio.to_thread(acceleration.artifact, identity)
        if content is None:
            raise HTTPException(410, "客户端探测已过期，请刷新订阅")
        return Response(content, media_type="application/java-archive",
                        headers={"Cache-Control": "no-store"})

    @router.post("/t4/acceleration/{identity}/ready")
    async def ready(identity: str, body: CapabilityProof):
        accepted = acceleration.confirm(identity, body.proof, body.protocol, body.version)
        return Response(
            '{"ready":' + ("true" if accepted else "false") + "}",
            media_type="application/json", headers={"Cache-Control": "no-store"},
        )

    @router.post("/t4/acceleration/{identity}/disable")
    async def disable(identity: str):
        acceleration.disable(identity)
        return {"ready": False}

    @router.get("/t4/acceleration/{identity}/plans/{plan_id}")
    async def plan(identity: str, plan_id: str, response: Response):
        value = acceleration.plan(identity, plan_id)
        if value is None:
            raise HTTPException(410, "播放任务已过期，请重新选集")
        response.headers["Cache-Control"] = "no-store"
        return value

    return router
