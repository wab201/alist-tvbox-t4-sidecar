from __future__ import annotations

import secrets
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OpaqueValue:
    source_id: str
    plugin_id: str
    kind: str
    value: str


class OpaqueIdStore:
    """Process-local public ID map; raw AT/plugin IDs never cross the T4 API."""

    def __init__(self) -> None:
        self._values: dict[str, OpaqueValue] = {}

    def put(self, value: OpaqueValue) -> str:
        token = "t4_" + secrets.token_urlsafe(18)
        self._values[token] = value
        return token

    def get(
        self,
        token: str,
        *,
        source_id: str,
        plugin_id: str,
        kind: str,
    ) -> str | None:
        value = self._values.get(token)
        if value is None:
            return None
        if (
            value.source_id != source_id
            or value.plugin_id != plugin_id
            or value.kind != kind
        ):
            return None
        return value.value
