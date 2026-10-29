from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass
import base64
import hashlib
import json
import sqlite3
import threading
import weakref


@dataclass(frozen=True, slots=True)
class OpaqueValue:
    source_id: str
    plugin_id: str
    kind: str
    value: str
    trace: str = ""


class OpaqueIdStore:
    """Bounded hot IDs with an automatically deleted, runtime-local disk backing."""

    def __init__(self, *, capacity: int = 2048, max_bytes: int = 4 * 1024 * 1024) -> None:
        self._values: OrderedDict[str, tuple[OpaqueValue, int]] = OrderedDict()
        self._capacity, self._max_bytes, self._size = capacity, max_bytes, 0
        self._salt = secrets.token_bytes(32)
        self._lock = threading.RLock()
        # An empty SQLite filename is a private, auto-deleted temporary database.
        self._db = sqlite3.connect("", check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA cache_size=-1024")
        self._db.execute("PRAGMA journal_mode=OFF")
        self._db.execute("PRAGMA synchronous=OFF")
        self._db.execute("PRAGMA temp_store=FILE")
        self._db.execute("CREATE TABLE ids (token TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._finalizer = weakref.finalize(self, self._db.close)

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
            "INSERT OR REPLACE INTO ids VALUES (?, ?)",
            ((token, json.dumps([
                value.source_id, value.plugin_id, value.kind, value.value, value.trace,
            ], ensure_ascii=True)) for token, (value, _) in entries),
        )

    def _lookup(self, token: str) -> OpaqueValue | None:
        with self._lock:
            hot = self._values.get(token)
            if hot is not None:
                self._values.move_to_end(token)
                return hot[0]
            row = self._db.execute("SELECT value FROM ids WHERE token=?", (token,)).fetchone()
            if row is None:
                return None
            value = OpaqueValue(*json.loads(row[0]))
            self._remember(token, value, len(row[0]) + len(token) + 256)
            return value

    def put(self, value: OpaqueValue) -> str:
        raw = json.dumps([
            value.source_id, value.plugin_id, value.kind, value.value, value.trace,
        ], ensure_ascii=True)
        digest = hashlib.blake2b(raw.encode("ascii"), key=self._salt, digest_size=18).digest()
        token = "t4_" + base64.urlsafe_b64encode(digest).decode("ascii")
        with self._lock:
            self._remember(token, value, len(raw) + len(token) + 256)
        return token

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
