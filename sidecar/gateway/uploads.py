from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from sidecar.at.subscription import PluginDescriptor, SubscriptionSnapshot
from sidecar.gateway.source_registry import SourceConfig


LOCAL_PLUGIN_SOURCE_ID = "local-uploaded"
SUPPORTED_SUFFIXES = {".py", ".js"}


class PluginUploadError(ValueError):
    """Raised when an uploaded plugin cannot be registered safely."""


class UploadedPluginStore:
    """Persist locally uploaded Python/JavaScript T4 plugins."""

    def __init__(self, root: str | Path, *, max_bytes: int = 2_000_000) -> None:
        self._root = Path(root)
        self._manifest = self._root / "registry.json"
        self._max_bytes = max_bytes
        self._plugins: dict[str, PluginDescriptor] = {}
        self._root.mkdir(parents=True, exist_ok=True)
        self._load()

    @property
    def source_config(self) -> SourceConfig:
        return SourceConfig(
            source_id=LOCAL_PLUGIN_SOURCE_ID,
            display_name="本地上传插件",
            status="ready",
            capabilities=("upload", "t4"),
        )

    def has_plugins(self) -> bool:
        return bool(self._plugins)

    def snapshot_for(self, source_id: str) -> SubscriptionSnapshot | None:
        if source_id != LOCAL_PLUGIN_SOURCE_ID:
            return None
        return SubscriptionSnapshot(
            source_id=source_id,
            site_count=len(self._plugins),
            plugins=tuple(self._plugins.values()),
        )

    def remove(self, plugin_id: str) -> bool:
        plugin = self._plugins.pop(plugin_id, None)
        if plugin is None:
            return False
        Path(plugin.loader).unlink(missing_ok=True)
        self._save()
        return True

    def register(self, filename: str | None, content: bytes) -> PluginDescriptor:
        suffix = Path(filename or "").suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise PluginUploadError("仅支持 .py 或 .js 爬虫文件")
        if not content:
            raise PluginUploadError("插件文件不能为空")
        if len(content) > self._max_bytes:
            raise PluginUploadError(
                f"插件文件超过 {self._max_bytes // 1_000_000} MB 限制"
            )

        plugin_id = f"upload_{hashlib.sha256(content).hexdigest()[:16]}"
        path = self._root / f"{plugin_id}{suffix}"
        temporary = self._root / f".{plugin_id}-upload{suffix}"
        temporary.write_bytes(content)
        try:
            self._validate_source(temporary, suffix)
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        name = _display_name(filename, plugin_id)
        plugin = _build_descriptor(plugin_id, name, suffix, path)
        self._plugins[plugin_id] = plugin
        self._save()
        return plugin

    def _load(self) -> None:
        if not self._manifest.is_file():
            return
        try:
            payload = json.loads(self._manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        entries = payload.get("plugins", []) if isinstance(payload, dict) else []
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            plugin_id = str(entry.get("id") or "")
            filename = str(entry.get("file") or "")
            suffix = Path(filename).suffix.lower()
            path = self._root / filename
            if (
                not plugin_id
                or suffix not in SUPPORTED_SUFFIXES
                or not path.is_file()
            ):
                continue
            self._plugins[plugin_id] = _build_descriptor(
                plugin_id,
                str(entry.get("name") or plugin_id),
                suffix,
                path,
            )

    def _save(self) -> None:
        payload = {
            "plugins": [
                {
                    "id": plugin.plugin_id,
                    "name": plugin.name,
                    "file": Path(plugin.loader).name,
                    "type": plugin.plugin_type,
                }
                for plugin in self._plugins.values()
            ]
        }
        temporary = self._manifest.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._manifest)

    @staticmethod
    def _validate_source(path: Path, suffix: str) -> None:
        source = path.read_text(encoding="utf-8")
        if suffix == ".py":
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError as exc:
                raise PluginUploadError(f"Python 语法错误：第 {exc.lineno} 行") from exc
            if not any(
                isinstance(node, ast.ClassDef) and node.name == "Spider"
                for node in ast.walk(tree)
            ):
                raise PluginUploadError("Python 插件必须定义 Spider 类")
            return

        node = shutil.which("node") or shutil.which("nodejs")
        if node is None:
            raise PluginUploadError("当前容器没有 JavaScript 运行时")
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode:
            raise PluginUploadError("JavaScript 语法检查失败")


def _build_descriptor(
    plugin_id: str,
    name: str,
    suffix: str,
    path: Path,
) -> PluginDescriptor:
    plugin_type = "raw_python" if suffix == ".py" else "javascript"
    api = "csp_PyProxy" if suffix == ".py" else "csp_JSProxy"
    return PluginDescriptor(
        plugin_id=plugin_id,
        name=name,
        plugin_type=plugin_type,
        api=api,
        source=str(path),
        loader=str(path),
        raw=True,
        token="",
        secret="",
        local_proxy_config={},
        data=None,
        filters=(),
    )


def _display_name(filename: str | None, plugin_id: str) -> str:
    stem = Path(filename or "").stem
    stem = re.sub(r"[\x00-\x1f/\\]+", "_", stem).strip()
    return stem[:80] or plugin_id
