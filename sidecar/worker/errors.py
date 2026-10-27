from __future__ import annotations

from pathlib import Path
import re
from typing import Any


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,100}$")
ERROR_FIELDS = ("root_error", "missing_module", "error_file", "error_function", "error_line",
                "errno", "winerror")


def error_details(exc: BaseException) -> dict[str, Any]:
    """Keep actionable exception metadata, never exception text or frame locals."""
    chain = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 12:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    root = chain[-1]
    details: dict[str, Any] = {"root_error": type(root).__name__}
    if isinstance(root, OSError):
        for key in ("errno", "winerror"):
            value = getattr(root, key, None)
            if isinstance(value, int):
                details[key] = value
    missing = next((item for item in chain if isinstance(item, ModuleNotFoundError)), None)
    if missing is not None and isinstance(missing.name, str) and _NAME.fullmatch(missing.name):
        details["missing_module"] = missing.name
    tb = root.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        filename = Path(filename.replace("\\", "/")).name
        if filename in {"<atvp-inner>", "<string>"}:
            filename = "plugin.py"
        for key, value in (
            ("error_file", filename), ("error_function", tb.tb_frame.f_code.co_name),
        ):
            if _NAME.fullmatch(value):
                details[key] = value
        details["error_line"] = tb.tb_lineno
    return details


def safe_error_fields(error: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key in ERROR_FIELDS:
        value = error.get(key)
        if key in {"error_line", "errno", "winerror"}:
            if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10_000_000:
                result[key] = value
        elif isinstance(value, str) and _NAME.fullmatch(value):
            result[key] = value
    return result


def failure_payload(code: str, exc: Exception) -> dict[str, Any]:
    details = safe_error_fields(error_details(exc))
    return {"code": code, "message": details.get("root_error", "RuntimeError"), **details}
