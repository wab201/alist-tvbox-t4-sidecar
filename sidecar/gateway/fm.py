from __future__ import annotations

import hashlib
import hmac
from urllib.parse import quote


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
) -> str | None:
    access_key = build_fm_access_key(gateway_token, source_id, plugin_id)
    if access_key is None:
        return None
    return (
        "/t4/fm/"
        f"{quote(source_id, safe='')}/"
        f"{quote(plugin_id, safe='')}/"
        f"{access_key}/"
    )
