from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import re
import threading
import time

from sidecar.diagnostics import record


class NextEpisodeWarmup:
    """Resolve only the adjacent episode from an observed same-line playlist."""

    def __init__(self, *, clock=time.monotonic, delay=0.25):
        self.clock, self.delay = clock, delay
        self.links = OrderedDict()
        self.results = OrderedDict()
        self.attempts = OrderedDict()
        self.lock = threading.RLock()
        self.task = None
        self.task_key = None
        self.running = False
        self.closed = False
        self.generation = 0

    @staticmethod
    def eligible(plugin, play_id):
        if not isinstance(play_id, str) or not play_id.strip() or len(play_id) > 8192:
            return False
        # IDs may be opaque, URLs or AT logical episodes. Reject commands, not runtimes.
        return not re.search(
            r"^(?:action|delete|remove|refresh|follow|unfollow|push|toast|javascript|"
            r"magnet|ed2k|thunder|atvp_detail|atvp_group)[:/-]|"
            r"^msub(?!ep-)[a-z]+-|[\x00-\x1f\x7f]",
            play_id, re.IGNORECASE,
        )

    @staticmethod
    def is_action(label):
        return bool(re.search(
            r"操作|管理|^(?:收藏|取消收藏|删除|检查更新|刷新|追更|取消追更|"
            r"订阅|取消订阅|关注|取消关注)|^(?:action|delete|refresh|follow|unfollow)\b",
            label.strip(), re.IGNORECASE,
        ))

    def remember(self, source, plugin, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("list"), list):
            return
        with self.lock:
            self.prune()
            for row in payload.get("list") or []:
                if not isinstance(row, dict):
                    continue
                if str(row.get("vod_tag") or "").lower() == "folder":
                    continue
                flags = str(row.get("vod_play_from") or "").split("$$$")
                groups = str(row.get("vod_play_url") or "").split("$$$")
                if len(flags) != len(groups):
                    continue
                for flag, group in zip(flags, groups):
                    entries = [entry.partition("$") for entry in group.split("#")]
                    ids = [value if sep and not self.is_action(label) else ""
                           for label, sep, value in entries]
                    for current in ids:
                        self.links.pop((source.source_id, plugin.plugin_id, flag, current), None)
                    if self.is_action(flag):
                        continue
                    for current, following in zip(ids, ids[1:]):
                        if (current != following and self.eligible(plugin, current)
                                and self.eligible(plugin, following)):
                            key = (source.source_id, plugin.plugin_id, flag, current)
                            self.links[key] = (self.clock(), plugin, following)
                            self.links.move_to_end(key)
                            while len(self.links) > 512:
                                self.links.popitem(last=False)

    @staticmethod
    def key(source, plugin, params, base, profile, *, context=None):
        # Hash all execution-affecting settings; never persist or log their credentials.
        value = [asdict(source), asdict(plugin), params, base,
                 profile.fingerprint if profile is not None else "", context]
        return hashlib.sha256(json.dumps(
            value, sort_keys=True, ensure_ascii=True, default=str,
        ).encode("ascii")).hexdigest()

    def take(self, key):
        with self.lock:
            self.prune()
            result = self.results.pop(key, None)
        if result is not None:
            record("playback.prewarm_hit")
            return deepcopy(result[1])
        return None

    async def take_running(self, key):
        # The same worker is already resolving this ID; don't parse it twice.
        if self.task_key == key and self.running and self.task is not None:
            await asyncio.shield(self.task)
        return self.take(key)

    def schedule(self, source, plugin, params, base, profile, resolve, *, context=None):
        if self.closed or not self.eligible(plugin, str(params.get("id") or "")):
            return
        with self.lock:
            self.prune()
            item = self.links.get((
                source.source_id, plugin.plugin_id,
                str(params.get("flag") or ""), str(params.get("id") or ""),
            ))
            if item is None or item[1] != plugin:
                return
            following = {**params, "id": item[2]}
            key = self.key(source, plugin, following, base, profile, context=context)
            if key in self.attempts or self.task is not None and not self.task.done():
                return
            self.attempts[key] = (self.clock(),)
            while len(self.attempts) > 16:
                self.attempts.popitem(last=False)
        self.task_key = key
        self.task = asyncio.create_task(self._warm(key, following, resolve, self.generation))

    async def _warm(self, key, params, resolve, generation):
        try:
            await asyncio.sleep(self.delay)
            if self.closed or self.generation != generation:
                return
            self.running = True
            record("playback.prewarm_start")
            result = await resolve(params)
            if result is None:
                record("playback.prewarm_skipped", reason="busy_or_context_changed")
                return
            url = result.get("url") if isinstance(result, dict) else None
            if (not isinstance(url, str) or not url.startswith(("http://", "https://"))
                    or result.get("parse", 0) not in {None, 0, "0"}
                    or result.get("jx", 0) not in {None, 0, "0"} or result.get("playUrl")):
                record("playback.prewarm_skipped", reason="not_resolved_media")
                return
            encoded = json.dumps(result, ensure_ascii=True).encode("ascii")
            if len(encoded) > 64 * 1024:
                record("playback.prewarm_skipped", reason="result_too_large")
                return
            with self.lock:
                if not self.closed and self.generation == generation:
                    self.results[key] = (self.clock(), deepcopy(result))
                    while len(self.results) > 16:
                        self.results.popitem(last=False)
                    record("playback.prewarm_ready")
                else:
                    record("playback.prewarm_skipped", reason="context_changed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record("playback.prewarm_failed", error=type(exc).__name__)
        finally:
            self.running = False

    def invalidate(self, source_id, plugin_id):
        with self.lock:
            self.generation += 1
            self.results.clear()
            self.attempts.clear()
            for key in tuple(self.links):
                if key[:2] == (source_id, plugin_id):
                    self.links.pop(key)

    def prune(self):
        with self.lock:
            removed = 0
            for store, ttl in ((self.links, 21600), (self.results, 45), (self.attempts, 45)):
                for key, item in tuple(store.items()):
                    if self.clock() - item[0] >= ttl:
                        store.pop(key)
                        removed += 1
            return removed

    async def aclose(self):
        self.closed = True
        if self.task is not None and not self.task.done():
            # Await the actual resolver; cancelling to_thread would leave it running.
            await asyncio.gather(self.task, return_exceptions=True)
        self.close()

    def close(self):
        with self.lock:
            self.closed = True
            self.links.clear()
            self.results.clear()
            self.attempts.clear()
