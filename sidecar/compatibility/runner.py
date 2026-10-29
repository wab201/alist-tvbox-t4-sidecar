from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import time
from typing import Any
import uuid

from pydantic import BaseModel, Field

from sidecar import __version__
from sidecar.diagnostics import TraceStore, new_trace_id, record, safe_label, trace_scope
from sidecar.t4.adapter import T4Request
from sidecar.compatibility.probes import ResourceProbes
from sidecar.playback import PlaybackError
from sidecar.worker.client import WorkerRemoteError
from sidecar.worker.errors import error_details, safe_error_fields


STAGES = ("home", "category", "search", "detail", "image", "play", "media")
STATUSES = ("pass", "fail", "blocked", "unsupported", "not_tested")


class CheckOptions(BaseModel):
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    plugin_ids: list[str] = Field(default_factory=list, max_length=1000)
    keyword: str = Field(default="", max_length=120)
    concurrency: int = Field(default=2, ge=1, le=3)
    category_limit: int = Field(default=2, ge=1, le=10)
    line_limit: int = Field(default=4, ge=1, le=12)
    navigation_depth: int = Field(default=8, ge=1, le=20)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def artifact_identity() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / "sidecar").rglob("*.py"))
    paths += sorted((root / "sidecar").rglob("*.mjs"))
    paths += [root / "sidecar/web/index.html", root / "pyproject.toml"]
    hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in paths if path.is_file()}
    return {"version": __version__, "source_sha256": hashes, "tree_sha256": digest(hashes),
            "python": platform.python_version(), "platform": platform.system()}


def result_status(stages: dict[str, Any]) -> str:
    values = [item["status"] for item in stages.values()]
    for status in ("fail", "blocked", "not_tested"):
        if status in values:
            return status
    return "unsupported" if "unsupported" in values else "pass"


def episodes(vod: dict[str, Any]) -> list[tuple[int, str, str] | None]:
    groups = str(vod.get("vod_play_url") or "").split("$$$")
    flags = str(vod.get("vod_play_from") or "").split("$$$")
    if len(groups) != len(flags):
        return [None]
    output = []
    for index, (flag, group) in enumerate(zip(flags, groups)):
        if flag.casefold() in {"操作", "互动", "关注", "actions", "action", "follow"}:
            continue
        if flag in {"未开播", "已下播"}:
            output.append(None)
            continue
        candidate = None
        for entry in group.split("#"):
            label, _, value = entry.partition("$")
            if not value or label.casefold().startswith((
                "检查", "刷新", "删除", "清空", "点赞", "投币", "收藏", "取消", "互动状态",
                "更新", "登录", "扫码", "绑定", "解绑", "关注",
                "delete", "refresh", "clear", "update", "login", "logout", "favorite",
                "unfavorite", "check", "bind", "unbind",
            )):
                continue
            candidate = (index + 1, flag, value)
            break
        if group:
            output.append(candidate)
    return output


class CompatibilityRunner:
    def __init__(
        self, sources, snapshot_for, adapter, bridge, traces: TraceStore, *,
        report_path: str | None = None, finish_runtime=None, release_plugin=None,
        proxy_registry=None,
    ):
        self.sources = sources
        self.snapshot_for = snapshot_for
        self.adapter = adapter
        self.bridge = bridge
        self.traces = traces
        self.probes = ResourceProbes(bridge, proxy_registry=proxy_registry)
        self.report_path = Path(report_path) if report_path else None
        self.finish_runtime = finish_runtime
        self.release_plugin = release_plugin
        self.artifact = artifact_identity()
        self.report: dict[str, Any] | None = None
        self.task: asyncio.Task | None = None
        self.stopping = False
        self._search_samples: dict[str, str] = {}
        self._save_lock = asyncio.Lock()
        if self.report_path and self.report_path.is_file():
            try:
                self.report = json.loads(self.report_path.read_text(encoding="utf-8"))
                if self.report["state"] in {"running", "cancelling"}:
                    self.report.update(state="interrupted", finished_at=time.time())
                    self.report["gate"]["passed"] = False
                    self.report["gate"]["reason"] = "process_interrupted"
            except (ValueError, KeyError, TypeError):
                self.report = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def playback_identity(self, source_ids=()) -> str:
        return digest([
            (source.source_id, source.cloud_playback, source.at_internal_url)
            for source in self.sources.all()
            if not source_ids or source.source_id in source_ids
        ])

    def start(self, options: CheckOptions, base: str) -> dict[str, Any]:
        if self.running:
            raise RuntimeError("already_running")
        selected = self.sources.all()
        if options.source_ids:
            if set(options.source_ids) - {item.source_id for item in selected}:
                raise ValueError("unknown_source")
            selected = tuple(item for item in selected if item.source_id in options.source_ids)
        self.stopping = False
        self._search_samples.clear()
        public_options = options.model_dump(exclude={"keyword"})
        public_options["keyword_mode"] = "explicit" if options.keyword else "derived_from_card_or_run_sample"
        self.report = {
            "schema_version": 1, "run_id": uuid.uuid4().hex, "state": "running",
            "started_at": time.time(), "artifact": deepcopy(self.artifact),
            "environment": "server_sample",
            "scope": "selected_plugins_sampled_capabilities" if options.plugin_ids else "all_plugins_sampled_capabilities",
            "options": public_options, "sources": [], "plugins": [],
            "errors": [],
            "progress": {"total": 0, "completed": 0},
            "gate": {"passed": False, "reason": "in_progress"},
            "real_device_verified": False, "long_play_verified": False,
            "remote_plugin_source_verified": False,
            "playback_sha256": self.playback_identity(options.source_ids),
        }
        self.task = asyncio.create_task(self._run(selected, options, base))
        return self.report

    def cancel(self) -> None:
        if self.running:
            self.stopping = True
            self.report["state"] = "cancelling"

    async def close(self) -> None:
        self.cancel()
        if self.task is not None:
            await self.task
        self.probes.close()

    def _save(self, content: str) -> None:
        if self.report_path:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.report_path.with_suffix(".tmp")
            temporary.write_text(content, encoding="utf-8")
            # Windows readers can temporarily deny replacement without sharing deletion.
            for delay in (.05, .1, .2, .4, None):
                try:
                    temporary.replace(self.report_path)
                    return
                except OSError as exc:
                    if getattr(exc, "winerror", None) not in {5, 32} or delay is None:
                        raise
                    time.sleep(delay)

    async def _persist(self) -> None:
        if self.report_path:
            async with self._save_lock:
                content = json.dumps(self.report, ensure_ascii=True, indent=2) + "\n"
                await asyncio.to_thread(self._save, content)

    def _run_error(self, exc: BaseException, phase: str, row=None) -> None:
        details = {"error": type(exc).__name__, **safe_error_fields(error_details(exc))}
        failure = {"phase": phase, **details}
        if row is not None:
            failure.update({key: row[key] for key in ("source_id", "plugin_id", "trace_id")})
        self.report["errors"].append(failure)
        self.report.setdefault("error", type(exc).__name__)
        self.traces.emit(
            "compatibility.failed",
            trace_id=row["trace_id"] if row else self.report["run_id"],
            reason=phase, **details,
            **({"source": row["source_id"], "plugin": row["plugin_id"]} if row else {}),
        )

    def _summary(self) -> None:
        report = self.report
        rows = report["plugins"]
        report["summary"] = dict(Counter(row["status"] for row in rows))
        report["stage_summary"] = {
            stage: dict(Counter(row["stages"][stage]["status"] for row in rows))
            for stage in STAGES
        }
        report["progress"] = {
            "total": len(rows), "completed": sum(row["state"] == "completed" for row in rows),
        }
        current = report.get("artifact_current", True) and report.get("playback_current", True)
        ready = [row for row in rows if not row.get("excluded_from_server_gate")]
        sources_ok = bool(report["sources"]) and all(source["status"] == "pass" for source in report["sources"])
        complete = report["state"] == "completed"
        passed = complete and current and sources_ok and bool(ready) and all(row["status"] == "pass" for row in ready)
        report["gate"] = {
            "passed": passed, "reason": "sample_gate_passed" if passed else
            ("playback_settings_changed" if not report.get("playback_current", True) else "artifact_changed") if not current else
            "in_progress" if report["state"] in {"running", "cancelling"} else
            "incomplete_or_failed" if complete else "run_" + report["state"],
            "eligible_plugins": len(ready),
            "excluded_plugins": len(rows) - len(ready),
            "scope": "sampled_server_capabilities_not_full_client_compatibility",
        }

    async def _run(self, sources, options, base) -> None:
        work = []
        requested = set(options.plugin_ids)
        matched = set()
        report = self.report
        terminal_state = "completed"
        phase = "subscription"
        try:
            for source in sources:
                if self.stopping:
                    report["sources"].append({"source_id": safe_label(source.source_id), "status": "not_tested"})
                    continue
                try:
                    snapshot = await self.snapshot_for(source)
                except Exception as exc:
                    report["sources"].append({
                        "source_id": safe_label(source.source_id), "status": "blocked", "error": type(exc).__name__,
                    })
                    continue
                source_row = {
                    "source_id": safe_label(source.source_id), "status": "pass",
                    "site_count": snapshot.site_count,
                    "descriptor_count": len(snapshot.plugins), "parse_errors": len(snapshot.errors),
                    "cloud_playback": source.cloud_playback,
                }
                report["sources"].append(source_row)
                if snapshot.errors or snapshot.site_count != len(snapshot.plugins):
                    source_row["status"] = "fail"
                seen = Counter(plugin.plugin_id for plugin in snapshot.plugins)
                for plugin in snapshot.plugins:
                    if requested and plugin.plugin_id not in requested:
                        continue
                    matched.add(plugin.plugin_id)
                    metadata = plugin.public_metadata()
                    classification = metadata.get("compatibility_mode", plugin.plugin_type)
                    # Only the one-way digest leaves this scope; no ext/data is serialized.
                    config = asdict(plugin)
                    row = {
                        "source_id": safe_label(source.source_id), "plugin_id": safe_label(plugin.plugin_id),
                        "name": safe_label(plugin.name), "classification": classification,
                        "configuration_sha256": digest(config),
                        "configuration_scope": "descriptor_digest_only_no_raw_values",
                        "state": "pending", "status": "not_tested", "trace_id": new_trace_id(),
                        "stages": {stage: {"status": "not_tested", "reason": "pending"} for stage in STAGES},
                    }
                    report["plugins"].append(row)
                    if metadata.get("t4_supported") is False:
                        row.update(state="completed", status="unsupported", excluded_from_server_gate=True)
                        row["stages"] = {stage: {"status": "unsupported", "reason": classification} for stage in STAGES}
                    elif seen[plugin.plugin_id] > 1:
                        row.update(state="completed", status="fail")
                        row["stages"]["home"] = {"status": "fail", "reason": "duplicate_plugin_id"}
                    else:
                        work.append((source, plugin, row))
                for unresolved in (() if requested else snapshot.unresolved_sites):
                    report["plugins"].append({
                        "source_id": safe_label(source.source_id),
                        "plugin_id": f"invalid-site-{unresolved['index']}",
                        "name": f"site-{unresolved['index']}", "classification": "invalid_descriptor",
                        "state": "completed", "status": "fail", "trace_id": "",
                        "stages": {stage: {"status": "fail" if stage == "home" else "not_tested",
                                           "reason": "invalid_descriptor"} for stage in STAGES},
                    })
            if requested - matched:
                report["errors"].append({
                    "phase": "selection", "error": "unknown_plugin",
                    "count": len(requested - matched),
                })
                terminal_state = "failed"
            self._summary()
            phase = "report_write"
            await self._persist()
            queue = iter(work)

            async def worker():
                row = None
                worker_phase = "check"
                try:
                    for source, plugin, row in queue:
                        worker_phase = "check"
                        if self.stopping:
                            row.update(state="completed", status="not_tested")
                            for stage in row["stages"].values():
                                stage["reason"] = "cancelled"
                            continue
                        row["state"] = "running"
                        start = time.monotonic()
                        with trace_scope(self.traces, trace_id=row["trace_id"],
                                         source=source.source_id, plugin=plugin.plugin_id):
                            try:
                                await self._check(source, plugin, row, options, base)
                            except Exception as exc:
                                row["stages"][row.get("active_stage", "home")] = {
                                    "status": "fail", "reason": "check_error",
                                    "error": type(exc).__name__, **safe_error_fields(error_details(exc)),
                                }
                            finally:
                                if (
                                    self.release_plugin and plugin.plugin_type != "builtin"
                                    and not plugin.t4_endpoint
                                ):
                                    try:
                                        await self.release_plugin(source.source_id, plugin.plugin_id)
                                    except Exception as exc:
                                        row["stages"]["home"] = {
                                            "status": "fail", "reason": "cleanup_error",
                                            "error": type(exc).__name__, **safe_error_fields(error_details(exc)),
                                        }
                                scored = {key: value for key, value in row["stages"].items()
                                          if value.get("reason") != "declared_search_disabled"}
                                row.update(state="completed", status=result_status(scored),
                                           duration_ms=round((time.monotonic() - start) * 1000))
                                record("compatibility.complete", status=row["status"], duration_ms=row["duration_ms"])
                                row["events"] = self.traces.events(trace_id=row["trace_id"], limit=100)
                        worker_phase = "summary"
                        self._summary()
                        worker_phase = "report_write"
                        await self._persist()
                except (Exception, asyncio.CancelledError) as exc:
                    self._run_error(exc, worker_phase, row)
                    raise

            phase = "worker"
            results = await asyncio.gather(*(worker() for _ in range(options.concurrency)), return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                terminal_state = "failed"
        except Exception as exc:
            terminal_state = "failed"
            self._run_error(exc, phase)
        finally:
            if self.finish_runtime:
                try:
                    await asyncio.to_thread(self.finish_runtime)
                except Exception as exc:
                    terminal_state = "failed"
                    self._run_error(exc, "runtime_cleanup")
            try:
                report["artifact_current"] = report["artifact"]["tree_sha256"] == artifact_identity()["tree_sha256"]
                report["playback_current"] = (
                    report["playback_sha256"] == self.playback_identity(options.source_ids)
                )
            except Exception as exc:
                terminal_state = "failed"
                report["artifact_current"] = False
                self._run_error(exc, "artifact_identity")
            report["state"] = "cancelled" if self.stopping and terminal_state == "completed" else terminal_state
            report["finished_at"] = time.time()
            self._summary()
            try:
                await self._persist()
            except Exception as exc:
                self._run_error(exc, "final_report_write")
                report["state"] = "failed"
                report["gate"].update(passed=False, reason="report_write_failed")

    async def _check(self, source, plugin, row, options, base):
        stages = row["stages"]

        async def call(stage: str, params: dict, *, module: str | None = None):
            row["active_stage"] = stage
            if self.stopping:
                stages[stage] = {"status": "not_tested", "reason": "cancelled"}
                return None
            start = time.monotonic()
            try:
                payload = await self.adapter.dispatch(T4Request(
                    module or stage, source.source_id, plugin.plugin_id, params, base,
                ))
                stages[stage] = {"status": "pass", "duration_ms": round((time.monotonic() - start) * 1000)}
                return payload
            except Exception as exc:
                details = (
                    exc.details if isinstance(exc, WorkerRemoteError)
                    else safe_error_fields(error_details(exc))
                )
                root_error = details.get("root_error", "")
                stages[stage] = {
                    "status": "blocked" if "Timeout" in type(exc).__name__ or "Timeout" in root_error else "fail",
                    "reason": "api_error", "error": type(exc).__name__,
                    **details,
                    **({"code": exc.code} if isinstance(exc, WorkerRemoteError) else {}),
                    "duration_ms": round((time.monotonic() - start) * 1000),
                }
                return None

        def cards(payload):
            return [item for item in (payload or {}).get("list", [])
                    if isinstance(item, dict) and item.get("vod_id") and item.get("vod_name")]

        home = await call("home", {})
        categories = (home or {}).get("class") or []
        if not isinstance(categories, list) or any(
            not isinstance(item, dict) or item.get("type_id") in (None, "") or not item.get("type_name")
            for item in categories
        ):
            stages["home"] = {"status": "fail", "reason": "invalid_categories"}
            categories = []
        if home is not None and not categories and not cards(home):
            stages["home"] = {"status": "blocked", "reason": "empty_home"}
        catalog_cards = cards(home)
        category_checks = []
        for category in categories[:options.category_limit]:
            listing = await call("category", {"t": str(category["type_id"]), "pg": 1})
            items = cards(listing)
            evidence = dict(stages["category"])
            if listing is not None:
                evidence.update(status="pass" if items else "blocked",
                                reason="cards_received" if items else "empty_category", count=len(items))
            category_checks.append(evidence)
            catalog_cards.extend(items)
        if category_checks:
            stages["category"] = {
                "status": result_status({str(i): item for i, item in enumerate(category_checks)}),
                "samples": category_checks, "published": len(categories),
                "tested": len(category_checks), "not_tested": max(0, len(categories) - len(category_checks)),
            }
        else:
            stages["category"] = {"status": "blocked", "reason": "no_category_sample"}
        # Never use synthetic category IDs or hand-built private detail IDs.
        if catalog_cards and source.source_id not in self._search_samples:
            self._search_samples[source.source_id] = str(catalog_cards[0]["vod_name"])
        keyword = options.keyword or (
            str(catalog_cards[0]["vod_name"]) if catalog_cards else self._search_samples.get(source.source_id, "")
        )
        keyword_origin = "explicit" if options.keyword else "plugin_card" if catalog_cards else "run_card"
        search_cards = []
        if plugin.site_options.get("searchable", 1) in {0, "0", False}:
            stages["search"] = {"status": "unsupported", "reason": "declared_search_disabled"}
        elif keyword:
            listing = await call("search", {"wd": keyword, "quick": False, "pg": 1})
            search_cards = cards(listing)
            if listing is not None:
                stages["search"].update(status="pass" if search_cards else "blocked",
                                        reason="cards_received" if search_cards else "empty_search",
                                        count=len(search_cards), sample_origin=keyword_origin)
        else:
            stages["search"] = {"status": "blocked", "reason": "no_keyword_sample"}
        # An explicit sample should test that result, not an unrelated home folder.
        candidates = (search_cards or catalog_cards) if options.keyword else (catalog_cards or search_cards)
        if not candidates:
            for stage in ("detail", "image", "play", "media"):
                stages[stage] = {"status": "blocked", "reason": "no_card_sample"}
            return
        selected = candidates[0]
        shortcut = None
        if selected.get("vod_tag") == "folder" and hasattr(self.adapter, "unwrap_id"):
            raw_id = self.adapter.unwrap_id(
                str(selected["vod_id"]), source_id=source.source_id,
                plugin_id=plugin.plugin_id, kind="detail",
            )
            if raw_id.startswith(("atvp_detail:", "atvp_group:")):
                # Test the real T4 detail shortcut, not only AT's folder navigation.
                shortcut = await call("detail", {"ac": "detail", "ids": selected["vod_id"]})
                if shortcut is None:
                    for stage in ("image", "play", "media"):
                        stages[stage] = {"status": "blocked", "reason": "detail_shortcut_failed"}
                    return
        navigation = []
        visited = set()
        navigation_reason = "navigation_depth_limit"
        for _ in range(options.navigation_depth):
            if shortcut is not None:
                break
            if selected.get("vod_tag") != "folder":
                break
            folder_id = str(selected["vod_id"])
            if hasattr(self.adapter, "unwrap_id"):
                folder_id = self.adapter.unwrap_id(
                    folder_id, source_id=source.source_id,
                    plugin_id=plugin.plugin_id, kind="detail",
                )
            if folder_id in visited:
                navigation_reason = "navigation_cycle_detected"
                break
            visited.add(folder_id)
            listing = await call("detail", {"t": selected["vod_id"], "pg": 1}, module="category")
            folder_cards = cards(listing)
            navigation.append({"module": "category", "count": len(folder_cards)})
            if not folder_cards:
                if listing is not None:
                    stages["detail"].update(status="blocked", reason="empty_folder")
                stages["detail"]["navigation"] = navigation
                stages["play"] = stages["media"] = {"status": "blocked", "reason": "no_detail_sample"}
                if not self.stopping:
                    stages["image"] = await asyncio.to_thread(self.probes.image, selected.get("vod_pic"))
                return
            selected = next((card for card in folder_cards if card.get("vod_tag") != "folder"), folder_cards[0])
        if shortcut is None and selected.get("vod_tag") == "folder":
            stages["detail"] = {"status": "not_tested", "reason": navigation_reason, "navigation": navigation}
            for stage in ("image", "play", "media"):
                stages[stage] = {"status": "blocked", "reason": "no_detail_sample"}
            return
        detail = shortcut if shortcut is not None else await call(
            "detail", {"ac": "detail", "ids": selected["vod_id"]},
        )
        if (detail or {}).get("t4_warnings"):
            stages["detail"].update(
                status="blocked", reason="cloud_lines_unavailable",
                unavailable_lines=len(detail["t4_warnings"]),
            )
        if navigation:
            stages["detail"]["navigation"] = navigation
        detail_cards = cards(detail)
        if detail is not None and not detail_cards:
            stages["detail"] = {"status": "fail", "reason": "empty_detail"}
        image = (detail_cards[0] if detail_cards else selected).get("vod_pic") or selected.get("vod_pic")
        if not self.stopping:
            row["active_stage"] = "image"
            stages["image"] = await asyncio.to_thread(self.probes.image, image)
        if not detail_cards:
            for stage in ("play", "media"):
                stages[stage] = {"status": "blocked", "reason": "no_detail_sample"}
            return
        lines = episodes(detail_cards[0])
        row["line_coverage"] = {"published": len(lines), "tested": 0,
                                "not_tested": max(0, len(lines) - options.line_limit)}
        checks = []
        for index, episode in enumerate(lines[:options.line_limit]):
            if episode is None:
                checks.append({"line": index + 1, "play": {"status": "blocked", "reason": "no_safe_episode"},
                               "media": {"status": "blocked", "reason": "no_safe_episode"}})
                continue
            _, flag, value = episode
            payload = await call("play", {"play": value, "flag": flag})
            play = dict(stages["play"])
            media = {"status": "blocked", "reason": "play_failed"}
            if payload is not None:
                if not payload.get("url"):
                    play.update(status="fail", reason="missing_play_url")
                elif not self.stopping:
                    row["active_stage"] = "media"
                    try:
                        payload = await asyncio.to_thread(self.bridge.prepare, payload, base)
                        media = await asyncio.to_thread(self.probes.media, payload)
                    except PlaybackError as exc:
                        media = {"status": "blocked" if exc.code in {
                            "server_parser_unresolved", "server_parser_dash_unsupported",
                            "server_parser_non_http",
                        } else "fail", "reason": exc.code,
                                 "real_device_verified": False}
            checks.append({"line": index + 1, "play": play, "media": media})
        row["line_coverage"]["tested"] = len(checks)
        if not checks:
            for stage in ("play", "media"):
                stages[stage] = {"status": "blocked", "reason": "no_safe_episode"}
        else:
            for stage in ("play", "media"):
                stages[stage] = {
                    "status": result_status({str(i): check[stage] for i, check in enumerate(checks)}),
                    "lines": [{"line": check["line"], **check[stage]} for check in checks],
                }
                if row["line_coverage"]["not_tested"] and stages[stage]["status"] == "pass":
                    stages[stage].update(status="not_tested", reason="line_limit_reached")
