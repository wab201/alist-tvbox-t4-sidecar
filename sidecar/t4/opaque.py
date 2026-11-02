from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import weakref


@dataclass(frozen=True, slots=True)
class OpaqueValue:
    source_id: str
    plugin_id: str
    kind: str
    value: str
    trace: str = ""
    metadata: dict[str, str] | None = None


class OpaqueIdStore:
    """Bounded hot IDs; optionally retain catalog identities across restarts."""

    def __init__(self, *, capacity: int = 2048, max_bytes: int = 4 * 1024 * 1024,
                 path: str | Path | None = None) -> None:
        self._values: OrderedDict[str, tuple[OpaqueValue, int]] = OrderedDict()
        self._capacity, self._max_bytes, self._size = capacity, max_bytes, 0
        self._salt = secrets.token_bytes(32)
        self._lock = threading.RLock()
        self._persistent = path is not None
        self._writes = 0
        if path is not None:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
        # Without a path, gate/probe IDs still use an auto-deleted database.
        self._db = sqlite3.connect(str(path) if path is not None else "",
                                   check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA cache_size=-1024")
        self._db.execute("PRAGMA journal_mode=" + ("WAL" if self._persistent else "OFF"))
        self._db.execute("PRAGMA synchronous=" + ("NORMAL" if self._persistent else "OFF"))
        self._db.execute("PRAGMA temp_store=FILE")
        self._db.execute("CREATE TABLE IF NOT EXISTS ids "
                         "(token TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)")
        if self._persistent:
            self._db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value BLOB)")
            self._db.execute("INSERT OR IGNORE INTO settings VALUES ('salt', ?)", (self._salt,))
            self._salt = self._db.execute("SELECT value FROM settings WHERE key='salt'").fetchone()[0]
            path.chmod(0o600)
            self._prune()
        self._finalizer = weakref.finalize(self, self._db.close)

    def _prune(self) -> None:
        self._db.execute("DELETE FROM ids WHERE updated < ?", (time.time() - 30 * 86400,))
        self._db.execute("DELETE FROM ids WHERE token IN "
                         "(SELECT token FROM ids ORDER BY updated DESC LIMIT -1 OFFSET 20000)")

    def _remember(self, token: str, value: OpaqueValue, size: int) -> None:
        previous = self._values.pop(token, None)
        if previous is not None:
            self._size -= previous[1]
        self._values[token] = value, size
        self._size += size
        while self._values and (
            len(self._values) > self._capacity or self._size > self._max_bytes
        ):
            cold_token, (cold_value, cold_size) = next(iter(self._values.items()))
            self._spill([(cold_token, (cold_value, cold_size))])
            self._values.pop(cold_token)
            self._size -= cold_size

    def _spill(self, entries) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO ids VALUES (?, ?, ?)",
            ((token, json.dumps([
                value.source_id, value.plugin_id, value.kind, value.value, value.trace,
                value.metadata,
            ], ensure_ascii=True), time.time()) for token, (value, _) in entries),
        )

    def _lookup(self, token: str) -> OpaqueValue | None:
        with self._lock:
            hot = self._values.get(token)
            if hot is not None:
                self._values.move_to_end(token)
                return hot[0]
            row = self._db.execute("SELECT value, updated FROM ids WHERE token=?", (token,)).fetchone()
            if row is None:
                return None
            if self._persistent and row[1] < time.time() - 30 * 86400:
                self._db.execute("DELETE FROM ids WHERE token=?", (token,))
                return None
            value = OpaqueValue(*json.loads(row[0]))
            self._remember(token, value, len(row[0]) + len(token) + 256)
            return value

    def put(self, value: OpaqueValue) -> str:
        raw = json.dumps([
            value.source_id, value.plugin_id, value.kind, value.value, value.trace,
            value.metadata,
        ], ensure_ascii=True)
        identity = json.dumps([
            value.source_id, value.plugin_id, value.kind, value.value,
        ], ensure_ascii=True) if value.kind == "detail" else raw
        digest = hashlib.blake2b(identity.encode("ascii"), key=self._salt, digest_size=18).digest()
        token = "t4_" + base64.urlsafe_b64encode(digest).decode("ascii")
        with self._lock:
            if self._persistent and value.kind == "detail":
                self._spill([(token, (value, len(raw)))])
                self._writes += 1
                if self._writes % 256 == 0:
                    self._prune()
            self._remember(token, value, len(raw) + len(token) + 256)
        return token

    def metadata(self, token: str, *, source_id: str, plugin_id: str) -> dict[str, str] | None:
        value = self._lookup(token)
        if value is None or (value.source_id, value.plugin_id, value.kind) != (
            source_id, plugin_id, "detail",
        ):
            return None
        return dict(value.metadata) if value.metadata is not None else None

    def get(
        self,
        token: str,
        *,
        source_id: str,
        plugin_id: str,
        kind: str,
    ) -> str | None:
        value = self._lookup(token)
        if value is None:
            return None
        if (
            value.source_id != source_id
            or value.plugin_id != plugin_id
            or value.kind != kind
        ):
            return None
        return value.value

    def origin_trace(self, token: str) -> str:
        value = self._lookup(token)
        return value.trace if value is not None else ""

    def release_memory(self) -> int:
        with self._lock:
            count = len(self._values)
            if not count:
                return 0
            with self._db:
                self._db.execute("BEGIN")
                self._spill(self._values.items())
            self._values.clear()
            self._size = 0
            self._db.execute("PRAGMA shrink_memory")
            return count

    def close(self) -> None:
        with self._lock:
            self._values.clear()
            self._size = 0
            self._finalizer()
