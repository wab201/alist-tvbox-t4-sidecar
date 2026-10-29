from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading

from sidecar.gateway.source_registry import SourceRegistry, validate_cloud_playback


class PlaybackSettingsStore:
    """Persist Sidecar-only overrides without writing the AT subscription/config."""

    def __init__(self, sources: SourceRegistry, path: Path) -> None:
        self.sources, self.path = sources, path
        self._lock = threading.Lock()
        self._overrides = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(self._overrides, dict):
            raise ValueError("播放配置必须是对象")
        for source_id, value in self._overrides.items():
            validate_cloud_playback(value["mode"], value.get("at_internal_url"))
            source = sources.get(source_id)
            if source is not None:
                sources.upsert(replace(
                    source, cloud_playback=value["mode"],
                    at_internal_url=value.get("at_internal_url") or None,
                ))

    def get(self, source_id: str) -> dict:
        source = self.sources.get(source_id)
        if source is None:
            raise KeyError(source_id)
        return {
            "mode": source.cloud_playback,
            "at_internal_url": source.at_internal_url or "",
        }

    def update(self, source_id: str, mode: str, internal_url: str) -> dict:
        internal_url = internal_url.strip().rstrip("/")
        validate_cloud_playback(mode, internal_url)
        with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise KeyError(source_id)
            updated = replace(source, cloud_playback=mode, at_internal_url=internal_url or None)
            values = {**self._overrides, source_id: {
                "mode": mode, "at_internal_url": internal_url,
            }}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent, delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump(values, stream, ensure_ascii=True, indent=2)
                    stream.write("\n")
                temporary.replace(self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self._overrides = values
            self.sources.upsert(updated)
            return self.get(source_id)
