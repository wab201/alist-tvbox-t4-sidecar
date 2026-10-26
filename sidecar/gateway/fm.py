from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any
from urllib.parse import quote

from sidecar.at.subscription import SubscriptionParseError


def build_config_access_key(gateway_token: str | None) -> str | None:
    if not gateway_token:
        return None
    return hmac.new(
        gateway_token.encode("utf-8"),
        b"t4:client-config",
        hashlib.sha256,
    ).hexdigest()


def config_access_key_matches(
    gateway_token: str | None, access_key: str | None
) -> bool:
    expected = build_config_access_key(gateway_token)
    return bool(expected and access_key) and hmac.compare_digest(expected, access_key)


def build_config_path(gateway_token: str | None) -> str:
    access_key = build_config_access_key(gateway_token)
    if access_key:
        return f"/t4/config/{access_key}.json"
    return "/t4/config.json"


def build_site_key(source_id: str, plugin_id: str) -> str:
    identity = json.dumps([source_id, plugin_id], separators=(",", ":"))
    return "t4_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def build_fm_access_key(
    gateway_token: str | None,
    source_id: str,
    plugin_id: str,
) -> str | None:
    if not gateway_token:
        return None
    message = f"{source_id}\x00{plugin_id}".encode("utf-8")
    return hmac.new(
        gateway_token.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def fm_access_key_matches(
    gateway_token: str | None,
    source_id: str,
    plugin_id: str,
    access_key: str,
) -> bool:
    expected = build_fm_access_key(gateway_token, source_id, plugin_id)
    return bool(expected) and hmac.compare_digest(expected, access_key)


def build_fm_path(
    gateway_token: str | None,
    source_id: str,
    plugin_id: str,
) -> str:
    access_key = build_fm_access_key(gateway_token, source_id, plugin_id)
    source_part = f"{quote(source_id, safe='')}/"
    plugin_part = f"{quote(plugin_id, safe='')}/"
    if access_key is None:
        return f"/t4/fm/{source_part}{plugin_part}"
    return (
        "/t4/fm/"
        f"{source_part}"
        f"{plugin_part}"
        f"{access_key}/"
    )


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
