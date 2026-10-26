from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import threading
from dataclasses import replace
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
        self._root = Path(root).resolve()
        self._manifest = self._root / "registry.json"
        self._max_bytes = max_bytes
        self._plugins: dict[str, PluginDescriptor] = {}
        self._lock = threading.RLock()
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
        with self._lock:
            return bool(self._plugins)

    def snapshot_for(self, source_id: str) -> SubscriptionSnapshot | None:
        if source_id != LOCAL_PLUGIN_SOURCE_ID:
            return None
        with self._lock:
            return SubscriptionSnapshot(
                source_id=source_id,
                site_count=len(self._plugins),
                plugins=tuple(self._plugins.values()),
            )

    def remove(self, plugin_id: str) -> bool:
        with self._lock:
            plugin = self._plugins.get(plugin_id)
            if plugin is None:
                return False
            plugins = dict(self._plugins)
            del plugins[plugin_id]
            self._save(plugins)
            self._plugins = plugins
            Path(plugin.loader).unlink(missing_ok=True)
            return True

    def rename(self, plugin_id: str, name: str) -> PluginDescriptor:
        name = name.strip()
        if not name or len(name) > 80 or re.search(r"[\x00-\x1f\x7f]", name):
            raise PluginUploadError("名称需为 1-80 个字符，不能包含控制字符")
        with self._lock:
            if plugin_id not in self._plugins:
                raise KeyError(plugin_id)
            if any(
                other.plugin_id != plugin_id and other.name.casefold() == name.casefold()
                for other in self._plugins.values()
            ):
                raise PluginUploadError("已有同名插件，请使用其他名称")
            plugin = replace(self._plugins[plugin_id], name=name)
            plugins = {**self._plugins, plugin_id: plugin}
            self._save(plugins)
            self._plugins = plugins
            return plugin

    def reorder(self, plugin_ids: list[str]) -> None:
        with self._lock:
            if len(plugin_ids) != len(self._plugins) or set(plugin_ids) != set(self._plugins):
                raise PluginUploadError("排序列表已变化，请刷新后重试；需包含全部插件且不能重复")
            plugins = {plugin_id: self._plugins[plugin_id] for plugin_id in plugin_ids}
            self._save(plugins)
            self._plugins = plugins

    def validate(self, plugin_id: str) -> PluginDescriptor:
        with self._lock:
            plugin = self._plugins[plugin_id]
            path = Path(plugin.loader)
            if not path.is_file():
                raise PluginUploadError("插件文件已丢失，请重新上传")
            self._validate_source(path, path.suffix)
            return plugin

    def register(self, filename: str | None, content: bytes) -> PluginDescriptor:
        with self._lock:
            return self._register(filename, content)

    def _register(self, filename: str | None, content: bytes) -> PluginDescriptor:
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
        if plugin_id in self._plugins:
            existing = self._plugins[plugin_id]
            if Path(existing.loader).is_file():
                return existing
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
        existing_names = {item.name.casefold() for item in self._plugins.values()}
        original_name = name
        index = 2
        while name.casefold() in existing_names:
            ending = f" ({index})"
            name = f"{original_name[:80 - len(ending)]}{ending}"
            index += 1
        plugin = _build_descriptor(plugin_id, name, suffix, path)
        # A repeated upload restores a missing file without losing its display name.
        plugin = self._plugins.get(plugin_id, plugin)
        plugins = {**self._plugins, plugin_id: plugin}
        self._save(plugins)
        self._plugins = plugins
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
                not re.fullmatch(r"upload_[0-9a-f]{16}", plugin_id)
                or suffix not in SUPPORTED_SUFFIXES
                or filename != f"{plugin_id}{suffix}"
                or not path.is_file()
            ):
                continue
            self._plugins[plugin_id] = _build_descriptor(
                plugin_id,
                str(entry.get("name") or plugin_id),
                suffix,
                path,
            )

    def _save(self, plugins: dict[str, PluginDescriptor]) -> None:
        payload = {
            "plugins": [
                {
                    "id": plugin.plugin_id,
                    "name": plugin.name,
                    "file": Path(plugin.loader).name,
                    "type": plugin.plugin_type,
                }
                for plugin in plugins.values()
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
        try:
            source = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PluginUploadError("插件文件必须使用 UTF-8 编码") from exc
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
        try:
            result = subprocess.run(
                [node, "--check", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            raise PluginUploadError("JavaScript 语法检查超时，请检查文件后重试") from exc
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
