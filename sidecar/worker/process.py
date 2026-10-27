from __future__ import annotations

import json
import contextlib
import os
import sys
import time
import uuid
from typing import Any

from sidecar.worker.atvp_runtime import AtvpRuntime, AtvpRuntimeError
from sidecar.worker.errors import failure_payload


SESSION_ID = uuid.uuid4().hex
ATVP_RUNTIME = AtvpRuntime()


def _response(
    request_id: str,
    *,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": request_id,
        "ok": error is None,
        "result": result,
        "error": error,
    }


def _handle(request: dict[str, Any]) -> dict[str, Any]:
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params", {})
    if not isinstance(request_id, str):
        return _response(
            "",
            error={"code": "invalid_request", "message": "id must be a string"},
        )
    if not isinstance(method, str) or not method:
        return _response(
            request_id,
            error={"code": "invalid_request", "message": "method is required"},
        )
    if not isinstance(params, dict):
        return _response(
            request_id,
            error={"code": "invalid_request", "message": "params must be an object"},
        )

    if method == "ping":
        return _response(request_id, result={"status": "ready"})
    if method == "inspect":
        return _response(
            request_id,
            result={
                "pid": os.getpid(),
                "session_id": SESSION_ID,
            },
        )
    if method == "echo":
        return _response(request_id, result=params)
    if method == "delay":
        seconds = params.get("seconds", 0)
        if not isinstance(seconds, (int, float)) or seconds < 0 or seconds > 30:
            return _response(
                request_id,
                error={"code": "invalid_request", "message": "invalid delay"},
            )
        time.sleep(seconds)
        return _response(request_id, result={"status": "complete"})
    if method == "init":
        try:
            extend = params.get("extend", "")
            if not isinstance(extend, str):
                raise AtvpRuntimeError("Atvp extend must be a string")
            site_key = str(params.get("site_key") or "")
            result = ATVP_RUNTIME.init(extend, site_key=site_key) if site_key else ATVP_RUNTIME.init(extend)
            return _response(request_id, result=result)
        except Exception as exc:
            return _response(
                request_id,
                error=failure_payload("plugin_init_failed", exc),
            )
    if method == "home":
        try:
            return _response(
                request_id,
                result=ATVP_RUNTIME.home(bool(params.get("filter", True))),
            )
        except Exception as exc:
            return _response(
                request_id,
                error=failure_payload("plugin_home_failed", exc),
            )
    if method == "category":
        try:
            return _response(
                request_id,
                result=ATVP_RUNTIME.category(
                    str(params.get("tid", "")),
                    str(params.get("pg", "1")),
                    bool(params.get("filter", True)),
                    params.get("extend", ""),
                ),
            )
        except Exception as exc:
            return _response(
                request_id,
                error=failure_payload("plugin_category_failed", exc),
            )
    if method == "detail":
        try:
            ids = params.get("ids", [])
            if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                raise AtvpRuntimeError("detail ids must be a string list")
            return _response(request_id, result=ATVP_RUNTIME.detail(ids))
        except Exception as exc:
            return _response(
                request_id,
                error=failure_payload("plugin_detail_failed", exc),
            )
    if method == "search":
        try:
            return _response(
                request_id,
                result=ATVP_RUNTIME.search(
                    str(params.get("key", "")),
                    bool(params.get("quick", True)),
                    str(params.get("pg", "1")),
                ),
            )
        except Exception as exc:
            return _response(
                request_id,
                error=failure_payload("plugin_search_failed", exc),
            )
    if method == "play":
        try:
            return _response(
                request_id,
                result=ATVP_RUNTIME.play(
                    str(params.get("flag", "")),
                    str(params.get("id", "")),
                    params.get("vip_flags", []),
                ),
            )
        except Exception as exc:
            return _response(
                request_id,
                error=failure_payload("plugin_play_failed", exc),
            )
    if method == "action":
        try:
            return _response(
                request_id,
                result=ATVP_RUNTIME.action(str(params.get("action", ""))),
            )
        except Exception as exc:
            return _response(
                request_id,
                error=failure_payload("plugin_action_failed", exc),
            )
    return _response(
        request_id,
        error={"code": "unsupported_method", "message": method},
    )


def _main(output) -> int:
    for line in sys.stdin:
        request_id = ""
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            if isinstance(request.get("id"), str):
                request_id = request["id"]
            response = _handle(request)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            response = _response(
                request_id,
                error=failure_payload("invalid_request", exc),
            )
        except Exception as exc:
            response = _response(
                request_id,
                error=failure_payload("worker_error", exc),
            )
        # Keep the pipe ASCII-only so the protocol is stable across Windows
        # code pages and Linux UTF-8 locales.
        output.write(json.dumps(response, ensure_ascii=True) + "\n")
        output.flush()
    return 0


def main() -> int:
    output = sys.stdout
    # Host HTTP handlers and plugin background threads must never write to the RPC pipe.
    with contextlib.redirect_stdout(sys.stderr):
        return _main(output)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        ATVP_RUNTIME.close()
