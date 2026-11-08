from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading


class MediaEngineSettings:
    """One persisted request-routing choice, never a restart of either runtime."""

    def __init__(self, path: Path, default: str, traces) -> None:
        self.path, self.traces = path, traces
        self._lock = threading.RLock()
        self._update_lock = threading.Lock()
        self.available = False
        values = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(values, dict):
            raise ValueError("Invalid persisted media engine settings")
        self.selected = values.get("engine", default)
        self.generation = values.get("generation", 0)
        if self.selected not in {"python", "rust"} or type(self.generation) is not int or self.generation < 0:
            raise ValueError("Invalid persisted media engine settings")

    def snapshot(self) -> dict:
        with self._lock:
            engine = self.selected if self.available else "python"
            return {
                "engine": engine, "selected_engine": self.selected,
                "generation": self.generation, "rust_available": self.available,
                "hot_switch": self.available, "ingress": "rust" if self.available else "python",
                "experimental": engine == "rust",
            }

    def update(self, engine: str) -> dict:
        if engine not in {"python", "rust"}:
            raise ValueError("Invalid media engine")
        # Serialize writers without making request snapshots wait for disk I/O.
        with self._update_lock:
            with self._lock:
                if engine == "rust" and not self.available:
                    raise RuntimeError("Rust media module is unavailable in this running service")
                if engine == self.selected:
                    return self.snapshot()
                previous = self.selected
                values = {"engine": engine, "generation": self.generation + 1}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(values, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            with self._lock:
                self.selected, self.generation = engine, values["generation"]
                result = self.snapshot()
            self.traces.emit("runtime.engine_changed", engine=engine, previous_engine=previous,
                             engine_generation=self.generation, reason="web")
            return result
