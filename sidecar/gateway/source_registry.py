from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SourceConfig:
    source_id: str
    display_name: str
    status: str = "unknown"
    capabilities: tuple[str, ...] = ()
    at_base_url: str | None = None
    subscription_id: str | None = None
    subscription_token: str | None = None
    subscription_url: str | None = None

    def public_metadata(self) -> dict[str, Any]:
        return {
            "id": self.source_id,
            "name": self.display_name,
            "status": self.status,
            "capabilities": list(self.capabilities),
        }


class SourceRegistry:
    def __init__(self, sources: tuple[SourceConfig, ...] = ()) -> None:
        self._sources = sources

    def upsert(self, source: SourceConfig) -> None:
        self._sources = tuple(
            source if item.source_id == source.source_id else item
            for item in self._sources
        )
        if not any(item.source_id == source.source_id for item in self._sources):
            self._sources += (source,)

    def remove(self, source_id: str) -> None:
        self._sources = tuple(
            item for item in self._sources if item.source_id != source_id
        )

    @classmethod
    def from_file(cls, path: str | None) -> "SourceRegistry":
        if not path:
            return cls()

        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_sources = payload.get("sources", payload) if isinstance(payload, dict) else payload
        if not isinstance(raw_sources, list):
            raise ValueError("source configuration must contain a list")

        sources = tuple(cls._parse_source(item) for item in raw_sources)
        return cls(sources)

    @staticmethod
    def _parse_source(item: Any) -> SourceConfig:
        if not isinstance(item, dict):
            raise ValueError("source entry must be an object")

        source_id = item.get("id")
        display_name = item.get("name")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("source entry requires a non-empty id")
        if not isinstance(display_name, str) or not display_name:
            raise ValueError("source entry requires a non-empty name")

        raw_capabilities = item.get("capabilities", [])
        if not isinstance(raw_capabilities, list) or not all(
            isinstance(value, str) for value in raw_capabilities
        ):
            raise ValueError("source capabilities must be a string list")

        status = item.get("status", "unknown")
        if not isinstance(status, str):
            raise ValueError("source status must be a string")

        at_base_url = item.get("at_base_url")
        if at_base_url is not None and not isinstance(at_base_url, str):
            raise ValueError("at_base_url must be a string")
        subscription_id = item.get("subscription_id")
        if subscription_id is not None and not isinstance(subscription_id, str):
            raise ValueError("subscription_id must be a string")
        subscription_token = item.get("subscription_token")
        if subscription_token is not None and not isinstance(subscription_token, str):
            raise ValueError("subscription_token must be a string")
        subscription_url = item.get("subscription_url")
        if subscription_url is not None and not isinstance(subscription_url, str):
            raise ValueError("subscription_url must be a string")

        return SourceConfig(
            source_id=source_id,
            display_name=display_name,
            status=status,
            capabilities=tuple(raw_capabilities),
            at_base_url=at_base_url,
            subscription_id=subscription_id,
            subscription_token=subscription_token,
            subscription_url=subscription_url,
        )

    def public_metadata(self) -> list[dict[str, Any]]:
        return [source.public_metadata() for source in self._sources]

    def get(self, source_id: str) -> SourceConfig | None:
        return next(
            (source for source in self._sources if source.source_id == source_id),
            None,
        )
