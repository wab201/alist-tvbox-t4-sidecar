from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    request_id: str
    method: str
    params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.request_id,
            "method": self.method,
            "params": self.params,
        }


@dataclass(frozen=True, slots=True)
class WorkerResponse:
    request_id: str
    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkerResponse":
        request_id = payload.get("id")
        if not isinstance(request_id, str):
            raise ValueError("worker response id must be a string")
        ok = payload.get("ok")
        if not isinstance(ok, bool):
            raise ValueError("worker response ok must be a boolean")
        error = payload.get("error")
        if error is not None and not isinstance(error, dict):
            raise ValueError("worker response error must be an object")
        return cls(
            request_id=request_id,
            ok=ok,
            result=payload.get("result"),
            error=error,
        )
