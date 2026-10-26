from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any
from urllib.parse import quote

from sidecar.at.subscription import SubscriptionParseError


def build_site_key(source_id: str, plugin_id: str) -> str:
    identity = json.dumps([source_id, plugin_id], separators=(",", ":"))
    return "t4_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def build_fm_path(
    source_id: str,
    plugin_id: str,
) -> str:
    source_part = f"{quote(source_id, safe='')}/"
    plugin_part = f"{quote(plugin_id, safe='')}/"
    return f"/t4/fm/{source_part}{plugin_part}"


def translate_fm_request(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    output = dict(params)
    action = str(output.get("ac") or "")
    if output.get("play") not in (None, ""):
        return "play", output
    if action == "action":
        return "action", output
    if output.get("ids") not in (None, "", []):
        return "detail", output
    if output.get("wd") not in (None, ""):
        return "search", output
    if output.get("t") not in (None, ""):
        if output.get("ext") not in (None, ""):
            output["extend"] = _decode_filter(output.pop("ext"))
        return "category", output
    if action not in {"", "list", "detail", "videolist"}:
        raise SubscriptionParseError("unsupported T4 action")
    return "home", output


def _decode_filter(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise SubscriptionParseError("T4 ext must be a filter object")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        try:
            compact = "".join(value.split())
            raw = base64.b64decode(
                compact + "=" * (-len(compact) % 4), altchars=b"-_", validate=True
            )
            decoded = json.loads(raw.decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise SubscriptionParseError("T4 ext must contain JSON filters") from exc
    if not isinstance(decoded, dict):
        raise SubscriptionParseError("T4 ext must be a filter object")
    return decoded
