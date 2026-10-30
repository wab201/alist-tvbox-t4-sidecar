from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import weakref


class BrowseContextStore:
    """Disk-backed breadcrumbs, not plugin payloads, for cold search/category workers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._db = None
        self._directory = None
        self._finalizer = None

    def _open(self) -> None:
        if self._db is not None:
            return
        self._directory = tempfile.TemporaryDirectory(prefix="t4-browse-")
        self._db = sqlite3.connect(
            self._directory.name + "/contexts.db", check_same_thread=False,
        )
        self._db.execute("PRAGMA cache_size=-512")
        self._db.execute("PRAGMA journal_mode=OFF")
        self._db.execute("PRAGMA synchronous=OFF")
        self._db.execute("PRAGMA temp_store=FILE")
        self._db.execute(
            "CREATE TABLE contexts (runtime TEXT, item TEXT, kind TEXT, request TEXT, generation INTEGER, "
            "PRIMARY KEY(runtime,item,kind))"
        )
        self._db.execute("CREATE TABLE roots (runtime TEXT PRIMARY KEY, request TEXT)")
        self._finalizer = weakref.finalize(self, self._dispose, self._db, self._directory)

    @staticmethod
    def _dispose(db, directory) -> None:
        db.close()
        directory.cleanup()

    def remember(self, runtime: str, method: str, params: dict, result, generation: int) -> None:
        if method not in {"home", "category", "search"} or not isinstance(result, dict):
            return
        rows = list(result.get("list") or [])
        if method == "home":
            rows.extend(
                {"vod_id": row["type_id"], "vod_tag": "folder"}
                for row in result.get("class") or []
                if isinstance(row, dict) and row.get("type_id") is not None
            )
        with self._lock:
            self._open()
            current = [method, params]
            root = self._db.execute("SELECT request FROM roots WHERE runtime=?", (runtime,)).fetchone()
            chain = json.loads(root[0]) if root and method != "home" else []
            if method == "category":
                parent = self._db.execute(
                    "SELECT request FROM contexts WHERE runtime=? AND item=? AND kind='category'",
                    (runtime, str(params.get("tid", ""))),
                ).fetchone()
                if parent:
                    chain = json.loads(parent[0])
            if current not in chain:
                chain.append(current)
            request = json.dumps(chain, ensure_ascii=True)
            entries = [
                (runtime, str(row["vod_id"]),
                 "category" if row.get("vod_tag") == "folder" else "detail",
                 request, generation) for row in rows
                if isinstance(row, dict) and row.get("vod_id") is not None
            ]
            with self._db:
                if method == "home":
                    self._db.execute("INSERT OR REPLACE INTO roots VALUES (?,?)", (runtime, request))
                self._db.executemany("INSERT OR REPLACE INTO contexts VALUES (?,?,?,?,?)", entries)

    def for_detail(self, runtime: str, ids: list, generation: int, *, kind: str = "detail") -> list:
        with self._lock:
            self._open()
            found = []
            for item in ids:
                row = self._db.execute(
                    "SELECT request FROM contexts WHERE runtime=? AND item=? AND kind=? AND generation!=?",
                    (runtime, str(item), kind, generation),
                ).fetchone()
                if row and row[0] not in found:
                    found.append(row[0])
            chain = []
            for request in found:
                for item in json.loads(request):
                    if item not in chain:
                        chain.append(item)
            return chain

    def restored(self, runtime: str, ids: list, generation: int, *, kind: str = "detail") -> None:
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE contexts SET generation=? WHERE runtime=? AND item=? AND kind=?",
                [(generation, runtime, str(item), kind) for item in ids],
            )

    def remove(self, runtime: str) -> None:
        with self._lock:
            if self._db is not None:
                with self._db:
                    self._db.execute("DELETE FROM contexts WHERE runtime=?", (runtime,))
                    self._db.execute("DELETE FROM roots WHERE runtime=?", (runtime,))

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._finalizer()
                self._db = None
                self._directory = None
