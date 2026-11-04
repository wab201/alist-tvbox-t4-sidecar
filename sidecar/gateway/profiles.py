from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile
import threading
import uuid

from sidecar.gateway.fm import build_site_key


class PluginPolicyDenied(ValueError):
    """The requested plugin is not enabled in this output profile."""


@dataclass(frozen=True, slots=True)
class T4Profile:
    id: str = "default"
    name: str = "默认订阅"
    enabled: bool = True
    mode: str = "all"
    whitelist: tuple[str, ...] = ()
    blacklist: tuple[str, ...] = ()

    def allows(self, source_id: str, plugin_id: str) -> bool:
        key = build_site_key(source_id, plugin_id)
        return self.enabled and key not in self.blacklist and (
            self.mode != "whitelist" or key in self.whitelist
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def public_metadata(self) -> dict:
        return {
            **asdict(self),
            "subscription_path": (
                "/t4/config.json" if self.id == "default"
                else f"/t4/profiles/{self.id}/config.json"
            ),
        }


class T4ProfileStore:
    """Output policies only; profiles are not credentials or worker instances."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._profiles = {"default": T4Profile()}
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            entries = payload["profiles"]
            if not isinstance(entries, list):
                raise ValueError("T4 出口配置必须为列表")
            for item in entries:
                profile = self._parse(item)
                self._profiles[profile.id] = profile

    @staticmethod
    def _parse(item: dict) -> T4Profile:
        identifier = item.get("id")
        name = item.get("name")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", identifier):
            raise ValueError("出口 ID 需为小写字母、数字、下划线或连字符")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(
            ord(char) < 32 or ord(char) == 127 for char in name
        ):
            raise ValueError("出口名称需为 1-80 个字符，不能包含控制字符")
        if type(item.get("enabled", True)) is not bool:
            raise ValueError("启用状态必须为布尔值")
        if item.get("mode", "all") not in {"all", "whitelist", "blacklist"}:
            raise ValueError("插件模式只能是 all、whitelist 或 blacklist")
        lists = {}
        for key in ("whitelist", "blacklist"):
            values = item.get(key, [])
            if not isinstance(values, (list, tuple)) or not all(
                isinstance(value, str) and re.fullmatch(r"t4_[0-9a-f]{64}", value)
                for value in values
            ):
                raise ValueError("插件名单必须使用完整的插件标识")
            lists[key] = tuple(dict.fromkeys(values))
        return T4Profile(
            id=identifier, name=name.strip(), enabled=item.get("enabled", True),
            mode=item.get("mode", "all"), **lists,
        )

    def all(self) -> tuple[T4Profile, ...]:
        with self._lock:
            return tuple(self._profiles.values())

    def get(self, identifier: str) -> T4Profile | None:
        with self._lock:
            return self._profiles.get(identifier)

    def save(self, identifier: str | None, values: dict) -> T4Profile:
        with self._lock:
            if identifier is None:
                if len(self._profiles) >= 64:
                    raise ValueError("最多保存 64 个 T4 出口")
                identifier = uuid.uuid4().hex[:16]
            elif identifier not in self._profiles:
                raise KeyError(identifier)
            profile = self._parse({**values, "id": identifier})
            self._persist({**self._profiles, identifier: profile})
            return profile

    def delete(self, identifier: str) -> None:
        with self._lock:
            if identifier == "default":
                raise ValueError("默认订阅不能删除，可修改插件范围或停用")
            if identifier not in self._profiles:
                raise KeyError(identifier)
            self._persist({key: value for key, value in self._profiles.items() if key != identifier})

    def _persist(self, profiles: dict[str, T4Profile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump({"profiles": [asdict(p) for p in profiles.values()]},
                          stream, ensure_ascii=True, indent=2)
                stream.write("\n")
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._profiles = profiles
