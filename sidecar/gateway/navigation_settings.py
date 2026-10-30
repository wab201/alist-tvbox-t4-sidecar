from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading

from sidecar.at.subscription import PluginDescriptor


def can_search(plugin: PluginDescriptor) -> bool:
    return (
        plugin.api not in {"csp_PianDan", "csp_Push"}
        and plugin.site_options.get("searchable", 1) not in {0, "0", False}
        and plugin.public_metadata().get("t4_supported", True)
    )


class NavigationSettingsStore:
    """Sidecar-local selection; None includes newly added searchable plugins."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._values = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(self._values, dict) or any(
            value is not None and (
                not isinstance(value, list) or not all(isinstance(v, str) for v in value)
            ) for value in self._values.values()
        ):
            raise ValueError("片单搜源配置必须是插件 ID 列表")

    def selection(self, source_id: str) -> list[str] | None:
        with self._lock:
            value = self._values.get(source_id)
            return list(value) if value is not None else None

    def update(self, source_id: str, plugin_ids: list[str] | None) -> None:
        with self._lock:
            values = {**self._values, source_id: (
                list(dict.fromkeys(plugin_ids)) if plugin_ids is not None else None
            )}
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
            self._values = values
