"""Bounded append-only search sessions for native directory pagination."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import secrets
import time

from sidecar.at.subscription import SubscriptionParseError


@dataclass
class SearchRun:
    key: str
    total: int
    deadline: float
    session: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    touched: float = field(default_factory=time.monotonic)
    rows: list = field(default_factory=list)
    seen: set = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    completed: int = 0
    failed: int = 0
    truncated: int = 0
    size: int = 0
    done: bool = False

    def append(self, rows, transform):
        for index, row in enumerate(rows):
            if len(self.rows) >= 500 or self.size + len(json.dumps(
                    row, ensure_ascii=True).encode("ascii")) > 768 * 1024:
                self.truncated += len(rows) - index
                break
            row = transform(row)
            identity = row.get("content_key") or row.get("vod_id")
            if identity in self.seen:
                continue
            size = len(json.dumps(row, ensure_ascii=True).encode("ascii"))
            if len(self.rows) >= 500 or self.size + size > 768 * 1024:
                self.truncated += 1
                continue
            self.rows.append(row)
            self.seen.add(identity)
            self.size += size
        self.changed.set()

    async def window(self, offset, limit, wait=2.0):
        self.touched = time.monotonic()
        until = min(self.deadline, self.touched + wait)
        while not self.done and len(self.rows) < offset + limit:
            remaining = until - time.monotonic()
            if remaining <= 0:
                break
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                break
        pending = not self.done and time.monotonic() < self.deadline
        if not pending and not self.rows and (self.failed or self.completed < self.total):
            raise SubscriptionParseError("片单搜源失败或超时，请稍后重新进入")
        # Keep one load-more position visible while outstanding sources can add rows.
        return {"list": self.rows[offset:offset + limit],
                "total": len(self.rows) + int(pending), "t4_search": {
                    "incremental": True, "session": self.session,
                    "completed": self.completed, "failed": self.failed,
                    "total": self.total, "pending": pending,
                    "partial": pending or self.completed + self.failed < self.total,
                    "truncated": self.truncated, "offset": offset,
                }}


class NavigationSearches:
    def __init__(self):
        self.runs = OrderedDict()

    def prune(self):
        now = time.monotonic()
        removed = 0
        for old, run in list(self.runs.items()):
            if run.done and now - run.touched >= 120:
                self.runs.pop(old)
                removed += 1
        return removed

    def get(self, key, session, offset, total, budget):
        now = time.monotonic()
        self.prune()
        if session:
            run = self.runs.get(session)
            if run is None or run.key != key:
                raise SubscriptionParseError("搜索会话已过期，请返回片单重新选片")
            self.runs.move_to_end(session)
            return run, False
        for run in self.runs.values():
            if run.key == key:
                return run, False
        if offset:
            raise SubscriptionParseError("搜索会话已过期，请返回片单重新选片")
        while len(self.runs) >= 8:
            old = next((s for s, r in self.runs.items() if r.done), None)
            if old is None:
                raise SubscriptionParseError("正在搜索的片单过多，请稍后重试")
            self.runs.pop(old)
        run = SearchRun(key, total, now + budget)
        self.runs[run.session] = run
        return run, True
