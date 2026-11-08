"""Opt-in, isolated multi-round subscription sampling; reports never contain media URLs."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import hashlib
import json
import logging
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import uvicorn

from scripts.benchmark_rust_real_media import relays
from sidecar.compatibility.probes import ProbeFailure, media_kind
from sidecar.compatibility.runner import CheckOptions, CompatibilityRunner
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.t4.adapter import T4Request


def rotated_payload(payload, module, sample):
    """Choose other published cards/episodes without constructing private identifiers."""
    if not isinstance(payload, dict):
        return payload
    payload = dict(payload)
    items = payload.get("list")
    if not isinstance(items, list) or not items:
        return payload
    if module in {"home", "category", "search"}:
        offset = sample % len(items)
        payload["list"] = items[offset:] + items[:offset]
    elif module == "detail":
        payload["list"] = []
        for item in items:
            if not isinstance(item, dict):
                payload["list"].append(item)
                continue
            item = dict(item)
            groups = str(item.get("vod_play_url") or "").split("$$$")
            rotated = []
            for group in groups:
                entries = group.split("#")
                offset = sample % len(entries)
                rotated.append("#".join(entries[offset:] + entries[:offset]))
            item["vod_play_url"] = "$$$".join(rotated)
            payload["list"].append(item)
    return payload


def throttled(exc):
    details = getattr(exc, "details", {}) or {}
    return getattr(exc, "status", None) == 429 or details.get("http_status") == 429


def safe_mime(value):
    value = str(value).split(";", 1)[0].strip().lower()
    return value if re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", value) else "unknown"


def audit_gate(report, expected_rounds):
    batches = report["rounds"]
    completed = len(batches) == expected_rounds and all(
        batch["state"] == "completed" for batch in batches
    ) and not report.get("stopped_for_throttle")
    return {
        "audit_completed": bool(completed),
        "passed": bool(completed and batches and all(batch["gate"]["passed"] for batch in batches)
                       and all(row.get("byte_relay", {}).get("status") != "fail"
                               for batch in batches for row in batch["plugins"])),
        "full_subscription_passed": False,
        "scope": "selected_server_samples_not_client_playback",
    }


class ObservedInvoker:
    def __init__(self, invoker, observations):
        self.invoker, self.observations = invoker, observations

    def __getattr__(self, name):
        return getattr(self.invoker, name)

    def call(self, runtime_key, plugin, method, params):
        started = time.perf_counter()
        observation = {"method": method, "runtime": plugin.plugin_type}
        try:
            result = self.invoker.call(runtime_key, plugin, method, params)
            observation["returned_type"] = type(result).__name__
            if isinstance(result, dict):
                items = result.get("list")
                observation["list_count"] = len(items) if isinstance(items, list) else None
                if isinstance(result.get("url"), str):
                    try:
                        observation["url_scheme"] = urlsplit(result["url"]).scheme
                    except ValueError:
                        observation["url_scheme"] = "invalid"
                    observation["parse_required"] = result.get("parse") not in (None, 0, "0")
            return result
        except Exception as exc:
            observation["error"] = type(exc).__name__
            raise
        finally:
            observation["backend_ms"] = (time.perf_counter() - started) * 1000
            self.observations.append(observation)


class SamplingAdapter:
    def __init__(self, adapter, sample, interval, stop):
        self.adapter, self.sample, self.interval, self.stop = adapter, sample, interval, stop
        self.operations = []

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    async def dispatch(self, request):
        await asyncio.sleep(self.interval)
        operation = {"module": request.module}
        value = request.params.get("play") or request.params.get("ids")
        if value:
            value = value[0] if isinstance(value, list) else value
            raw = self.adapter.unwrap_id(
                str(value), source_id=request.source_id, plugin_id=request.plugin_id,
                kind="play" if request.module == "play" else "detail",
            )
            operation["identity_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
        started = time.perf_counter()
        invokers = {
            key: getattr(self.adapter, key) for key in ("_worker", "_native")
            if getattr(self.adapter, key, None) is not None
        }
        operation["backend"] = []
        for key, invoker in invokers.items():
            setattr(self.adapter, key, ObservedInvoker(invoker, operation["backend"]))
        try:
            result = await self.adapter.dispatch(request)
        except Exception as exc:
            operation.update(error=type(exc).__name__, status=getattr(exc, "status", None))
            if getattr(exc, "reason", None):
                operation["native_reason"] = exc.reason
            for marker, reason in (
                ("result must be an object", "non_object_plugin_result"),
                ("播放标识已失效", "expired_opaque_id"),
            ):
                if marker in str(exc):
                    operation["reason"] = reason
            if throttled(exc):
                self.stop()
            raise
        finally:
            for key, invoker in invokers.items():
                setattr(self.adapter, key, invoker)
            operation["dispatch_ms_without_pacing"] = (time.perf_counter() - started) * 1000
            self.operations.append(operation)
        return rotated_payload(result, request.module, self.sample)


async def prefix_read(client, url, headers, limit=262144, *, opaque_verified=False, trace=None,
                      complete_range=False):
    """A 200 prefix is valid reachability, not evidence of working seek/Range."""
    started = time.perf_counter()
    result = {"bytes": 0, "passed": False, "limit": limit, "response_exhausted": False}
    if trace is not None:
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", trace) is None:
            raise ValueError("invalid diagnostic trace")
        headers = {**headers, "x-t4-relay-client-trace": trace}
        result.update(trace=trace, client_start_us=time.time_ns() // 1000)
    digest = hashlib.sha256()
    try:
        async with client.stream("GET", url, headers={
            **headers, "Accept-Encoding": "identity", "Range": f"bytes=0-{limit - 1}",
        }) as response:
            result.update(
                status=response.status_code, headers_ms=(time.perf_counter() - started) * 1000,
                redirects=len(response.history),
                mime=safe_mime(response.headers.get("content-type", "")),
            )
            if trace is not None:
                result["client_headers_us"] = time.time_ns() // 1000
            if response.status_code not in {200, 206}:
                result["reason"] = "http_status"
                return result
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                 response.headers.get("content-range", ""))
            if response.status_code == 200 and response.headers.get("content-range"):
                result["reason"] = "range_on_200"
                return result
            if response.status_code == 206 and (
                match is None or int(match[1]) != 0 or int(match[2]) < int(match[1])
                or int(match[2]) >= int(match[3])
            ):
                result["reason"] = "invalid_range"
                return result
            result["range_metadata"] = [int(value) for value in match.groups()] if match else None
            length = response.headers.get("content-length", "")
            total = int(match[3]) if match else int(length) if length.isdigit() else None
            target = min(limit, total) if total is not None else limit
            if complete_range and (match is None or int(match[2]) + 1 != target):
                result["reason"] = "complete_range_required"
                return result
            if response.headers.get("content-encoding", "identity") != "identity":
                result["reason"] = "encoded_payload"
                return result
            prefix = b""
            async for chunk in response.aiter_raw():
                if not chunk:
                    continue
                result.setdefault("ttfb_ms", (time.perf_counter() - started) * 1000)
                if trace is not None and "client_first_body_us" not in result:
                    result["client_first_body_us"] = time.time_ns() // 1000
                kept = chunk[:target - result["bytes"]]
                if complete_range and len(kept) != len(chunk):
                    result["reason"] = "range_body_overflow"
                    return result
                if len(prefix) < 4096:
                    prefix += kept[:4096 - len(prefix)]
                digest.update(kept)
                result["bytes"] += len(kept)
                if result["bytes"] == target and not complete_range:
                    break
            else:
                result["response_exhausted"] = True
            result.update(sha256=digest.hexdigest(), format=media_kind(prefix),
                          range_supported=bool(match), truncated_prefix=result["bytes"] == limit,
                          opaque_verified_by_hls_probe=opaque_verified and not media_kind(prefix))
            expected = int(match[2]) + 1 if match else None
            if match and (expected != target or result["bytes"] != target):
                result["reason"] = "range_length_mismatch"
                return result
            if not result["bytes"]:
                result["reason"] = "empty_body"
                return result
            if ("html" in result["mime"] or "json" in result["mime"]
                    or prefix.lstrip().startswith((b"<!DOCTYPE", b"<html", b"#EXTM3U"))):
                result["reason"] = "not_byte_media"
                return result
            if result["bytes"] != target or not (result["format"] or opaque_verified):
                result["reason"] = "short_or_unknown_media"
                return result
            result["passed"] = True
            result["elapsed_ms"] = (time.perf_counter() - started) * 1000
            result["prefix_mib_per_second"] = (
                result["bytes"] / 1024 ** 2 / (result["elapsed_ms"] / 1000)
            )
    except Exception as exc:
        result.update(error=type(exc).__name__, reason="network_error")
    return result


class SampleRunner(CompatibilityRunner):
    def __init__(self, original, *, sample, interval, journal, relay_compare):
        super().__init__(
            original.sources, original.snapshot_for, original.adapter, original.bridge,
            original.traces, finish_runtime=original.finish_runtime,
            release_plugin=original.release_plugin,
            proxy_registry=original.probes.proxy_registry,
        )
        self.interval, self.journal, self.relay_compare = interval, journal, relay_compare
        self.active_asset = None
        self.capture = False
        self.throttle_seen = False
        self.journaled = set()
        self.resource_requests = []
        self.adapter = SamplingAdapter(original.adapter, sample, interval, self.stop_throttle)
        fetch = self.probes._fetch
        probe = self.probes.media

        def observed_fetch(url, headers=None, *, limit=262144, start=0):
            time.sleep(self.interval)
            begun = time.perf_counter()
            try:
                data, rh, final, status = fetch(url, headers, limit=limit, start=start)
            except ProbeFailure as exc:
                self.resource_requests.append({
                    "reason": exc.result.get("reason"), "http_status": exc.result.get("http_status"),
                    "duration_ms": (time.perf_counter() - begun) * 1000,
                })
                if exc.result.get("http_status") == 429:
                    self.stop_throttle()
                raise
            self.resource_requests.append({
                "http_status": status, "bytes": len(data), "offset": start,
                "duration_ms": (time.perf_counter() - begun) * 1000,
                "mime": safe_mime(rh.get("content-type", "")),
                "format": media_kind(data),
                "manifest": data.lstrip().startswith(b"#EXTM3U"),
                "html": data.lstrip().lower().startswith((b"<!doctype", b"<html")),
            })
            if self.capture and self.active_asset is None and start == 0 and data:
                # Compare exactly the delivered byte endpoint, including existing normalization.
                # HLS manifest rewriting is validated separately by ResourceProbes, not by Rust.
                if media_kind(data) or (not data.lstrip().startswith((b"#EXTM3U", b"<"))
                                        and rh.get("content-type", "").startswith("video/")):
                    self.active_asset = {"url": url, "headers": dict(headers or {})}
            return data, rh, final, status

        def observed_media(payload):
            self.capture = True
            try:
                return probe(payload)
            finally:
                self.capture = False

        self.probes._fetch = observed_fetch
        self.probes.media = observed_media

    def stop_throttle(self):
        self.throttle_seen = True
        self.cancel()

    async def _check(self, source, plugin, row, options, base):
        self.active_asset = None
        self.adapter.operations.clear()
        self.resource_requests.clear()
        try:
            await super()._check(source, plugin, row, options, base)
        finally:
            row["operations"] = list(self.adapter.operations)
            row["diagnostic_pacing_seconds"] = self.interval
            row["resource_requests"] = list(self.resource_requests)
            row["sampled_media_passed"] = any(
                line.get("status") == "pass" for line in row["stages"]["media"].get("lines", [])
            )
        row["sample_round"] = self.adapter.sample + 1
        if (self.active_asset is not None and self.relay_compare and not self.throttle_seen
                and row["sampled_media_passed"]):
            encrypted = any(
                segment.get("encryption") == "AES-128" and segment.get("decrypted_prefix")
                for line in row["stages"]["media"].get("lines", []) if line["status"] == "pass"
                for segment in line.get("segments", [])
            )
            row["byte_relay"] = await self.compare(self.active_asset, opaque_verified=encrypted)
        else:
            row["byte_relay"] = {
                "status": "not_tested",
                "reason": "no_media_sample" if self.active_asset is None else "comparison_disabled",
            }
        self.active_asset = None

    async def _persist(self):
        for row in self.report["plugins"]:
            identity = (row["source_id"], row["plugin_id"])
            if row["state"] != "completed" or identity in self.journaled:
                continue
            with self.journal.open("a", encoding="utf-8") as output:
                output.write(json.dumps(row, ensure_ascii=True) + "\n")
            self.journaled.add(identity)
            print(json.dumps({
                "round": row.get("sample_round", self.adapter.sample + 1),
                "plugin": row["plugin_id"], "name": row["name"], "status": row["status"],
                "stages": {key: value["status"] for key, value in row["stages"].items()},
                "relay": row.get("byte_relay", {}).get("status", "not_tested"),
            }, ensure_ascii=True), flush=True)
        self.journal.with_name(
            self.journal.stem + f".round-{self.adapter.sample + 1}.partial.json",
        ).write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    async def compare(self, asset, *, opaque_verified=False):
        result = {
            "status": "fail", "scope": "finite_delivered_byte_endpoint_not_full_playback",
            "via_candidate_gateway": urlsplit(asset["url"]).hostname in {"127.0.0.1", "localhost", "::1"},
        }
        try:
            # Child startup and affinity are blocking; keep the candidate gateway responsive.
            context = relays(asset)
            instances = await asyncio.to_thread(context.__enter__)
            try:
                async with httpx.AsyncClient(
                    trust_env=False, follow_redirects=True, timeout=25,
                ) as client:
                    result["reference"] = await prefix_read(
                        client, asset["url"], asset["headers"], opaque_verified=opaque_verified,
                    )
                    reference = result["reference"]
                    if reference.get("status") == 429:
                        self.stop_throttle()
                        return result
                    if not reference["passed"]:
                        return result
                    order = ("python", "rust") if self.adapter.sample % 2 == 0 else ("rust", "python")
                    for mode in order:
                        await asyncio.sleep(self.interval)
                        measured = await prefix_read(
                            client, instances[mode].url + "/t4/media/real", {},
                            opaque_verified=opaque_verified,
                        )
                        measured["hash_match"] = measured.get("sha256") == reference["sha256"]
                        measured["contract_match"] = all(
                            measured.get(key) == reference.get(key)
                            for key in ("status", "range_metadata", "mime", "bytes")
                        )
                        result[mode] = measured
                        if measured.get("status") == 429:
                            self.stop_throttle()
                            return result
                        if (not measured["passed"] or not measured["hash_match"]
                                or not measured["contract_match"]):
                            return result
                    deadline = time.monotonic() + 2
                    while True:
                        health = (await client.get(instances["rust"].url + "/health")).json()
                        if health["active"] == 0 or time.monotonic() >= deadline:
                            break
                        await asyncio.sleep(.02)
                    result["rust_health"] = health
                    result["status"] = "pass" if health["active"] == 0 else "fail"
            finally:
                await asyncio.to_thread(context.__exit__, None, None, None)
        except Exception as exc:
            result["error"] = type(exc).__name__
        return result


async def run(args, *, runner_factory=SampleRunner, configure_app=None):
    report = {
        "scope": "selected_plugins_multi_round_server_samples",
        "real_device_verified": False, "production_ready": False,
        "rounds": [], "selections": args.select,
    }
    with tempfile.TemporaryDirectory(prefix="t4-multi-") as directory:
        settings = replace(
            Settings.from_env(), plugin_dir=directory, source_config_file=None, log_file=None,
            compatibility_report=None, worker_timeout=25,
        )
        app = create_app(settings)
        if configure_app is not None:
            configure_app(app)
        async with app.router.lifespan_context(app):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                base = f"http://127.0.0.1:{listener.getsockname()[1]}"
                server = uvicorn.Server(uvicorn.Config(
                    app, lifespan="off", access_log=False, log_level="critical",
                ))
                serving = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    while not server.started:
                        if serving.done():
                            await serving
                            raise RuntimeError("gateway_not_started")
                        await asyncio.sleep(.01)
                    original = app.state.compatibility
                    snapshots = {
                        source.source_id: await original.snapshot_for(source)
                        for source in original.sources.all()
                    }
                    report["subscription_sites"] = sum(s.site_count for s in snapshots.values())
                    for index in range(args.rounds):
                        runner = runner_factory(
                            original, sample=index, interval=args.interval,
                            journal=args.output.with_suffix(".journal.jsonl"),
                            relay_compare=not args.no_relay,
                        )
                        try:
                            order = args.select[index % len(args.select):] + args.select[:index % len(args.select)]
                            ranks = {value.partition(":")[2]: rank for rank, value in enumerate(order)}
                            async def ordered_snapshot(source):
                                snapshot = snapshots[source.source_id]
                                return replace(snapshot, plugins=tuple(sorted(
                                    snapshot.plugins,
                                    key=lambda plugin: ranks.get(plugin.plugin_id, len(ranks)),
                                )))
                            runner.snapshot_for = ordered_snapshot
                            runner.start(CheckOptions(
                                source_ids=list(snapshots),
                                plugin_ids=[value.partition(":")[2] for value in order],
                                concurrency=1, category_limit=1, line_limit=1, navigation_depth=5,
                            ), base)
                            await runner.task
                            report["rounds"].append(runner.report)
                            report["stopped_for_throttle"] = runner.throttle_seen
                            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                            if runner.throttle_seen:
                                break
                        finally:
                            await runner.close()
                        await asyncio.sleep(args.interval)
                finally:
                    server.should_exit = True
                    await serving
    report["summary"] = dict(Counter(
        row["status"] for batch in report["rounds"] for row in batch["plugins"]
    ))
    report["completed_rounds"] = len(report["rounds"])
    report["gate"] = audit_gate(report, args.rounds)
    report["media_passed"] = sum(
        row.get("sampled_media_passed", False)
        for batch in report["rounds"] for row in batch["plugins"]
    )
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        key: report[key] for key in ("summary", "completed_rounds", "media_passed", "stopped_for_throttle")
    }), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--select", action="append", required=True,
                        help="Repeat category:plugin_id; category is cloud, ordinary or short.")
    parser.add_argument("--rounds", type=int, choices=(2, 3, 4), default=3)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--no-relay", action="store_true")
    args = parser.parse_args()
    if not .5 <= args.interval <= 5 or any(
        value.partition(":")[0] not in {"cloud", "ordinary", "short"}
        or re.fullmatch(r"[\w.-]{1,100}", value.partition(":")[2]) is None for value in args.select
    ):
        parser.error("invalid interval or selection")
    if args.output.exists() or args.output.with_suffix(".journal.jsonl").exists():
        parser.error("use a new output path; prior evidence must not be overwritten")
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        previous = json.loads(args.output.read_text(encoding="utf-8")) if args.output.is_file() else {}
        previous["harness_error"] = type(exc).__name__
        args.output.write_text(json.dumps(previous, indent=2), encoding="utf-8")
        print(json.dumps({"harness_error": type(exc).__name__}), flush=True)
        return 1
    return 0 if report["gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
