"""Opt-in real AT media experiment; bounded bytes, no URLs or credentials in output."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import statistics
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psutil

from scripts.benchmark_rust_media import MemorySampler, cpu_seconds, memory, percentile
from tests.rust.relay_support import Child, MIB, binary_path


def range_metadata(response, offset, size):
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                         response.headers.get("content-range", ""))
    valid = bool(
        response.status_code == 206 and match
        and int(match[1]) == offset and int(match[2]) == offset + size - 1
        and int(match[3]) >= offset + size
        and response.headers.get("content-length") == str(size)
        and response.headers.get("content-encoding", "identity") == "identity"
    )
    return {"status": response.status_code, "range_valid": valid,
            "object_bytes": int(match[3]) if match else None,
            "content_type": response.headers.get("content-type", "").split(";")[0][:80]}


def classify(url):
    parsed = urlsplit(url)
    return {"scheme": parsed.scheme, "at_proxy": parsed.path.startswith("/p/"),
            "sidecar_proxy": parsed.path.startswith(("/t4/media/", "/t4/proxy/")),
            "hls": parsed.path.lower().endswith(".m3u8")}


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def resolve(report, *, response_observer=None):
    from sidecar.at.native import NativeAtClient
    from sidecar.at.subscription import resolve_subscription
    from sidecar.gateway.source_registry import SourceConfig

    source = SourceConfig("env-at", "authorized", subscription_url=os.environ["ALIST_TVBOX_SUBSCRIPTION_URL"])
    started = time.perf_counter()
    snapshot = asyncio.run(resolve_subscription(source))
    plugin = snapshot.plugin_for("csp_XiaoYa")
    report.update({"subscription_ms": (time.perf_counter() - started) * 1000,
              "site_count": snapshot.site_count, "plugin": "csp_XiaoYa",
              "official_endpoint_present": bool(plugin.t4_endpoint), "stages": []})
    native = NativeAtClient(timeout=45)
    if response_observer is not None:
        native._client.event_hooks["response"].append(response_observer)

    def call(method, params):
        begin = time.perf_counter()
        item = {"operation": method}
        report["stages"].append(item)
        try:
            return native.call(plugin, method, params)
        except Exception as exc:
            item.update(error=type(exc).__name__, status=getattr(exc, "status", None),
                        reason=getattr(exc, "reason", None),
                        cause=type(exc.__cause__).__name__ if exc.__cause__ else None)
            raise
        finally:
            item["elapsed_ms"] = (time.perf_counter() - begin) * 1000

    try:
        keyword = "\u5ac1\u91d1\u9497"
        cards = call("search", {"key": keyword, "quick": False, "pg": "1"})["list"]
        matches = [v for v in cards if v.get("vod_name") == keyword]
        report["matching_cards"] = len(matches)
        vod = None
        for card in matches[:3]:
            raw_id = str(card["vod_id"])
            candidate = {"id_sha256": hashlib.sha256(str(card["vod_id"]).encode()).hexdigest(),
                         "id_chars": len(str(card["vod_id"])),
                         "folder": card.get("vod_tag") == "folder",
                         "media_subscription_id": raw_id.startswith("msub:"),
                         "msearch_id": raw_id.startswith("msearch:"),
                         "has_site_path": "$" in raw_id or "%24" in raw_id,
                         "numeric_site_and_id": bool(re.fullmatch(r"\d+(?:\$|%24)\d+", raw_id))}
            report.setdefault("detail_candidates", []).append(candidate)
            try:
                if raw_id.startswith("msub:"):
                    plugin = snapshot.plugin_for("csp_Media")
                    candidate["detail_plugin"] = "csp_Media"
                listing = call("detail", {"ids": [card["vod_id"]]})
                selected = next(iter(listing.get("list") or []), {})
                if selected.get("vod_play_url"):
                    vod = selected
                    candidate["playlist"] = True
                    break
                candidate["playlist"] = False
            except Exception as exc:
                candidate.update(error=type(exc).__name__, status=getattr(exc, "status", None))
                if getattr(exc, "status", None) == 429:
                    raise
        if vod is None:
            plugin = snapshot.plugin_for("csp_Media")
            media_cards = call("search", {"key": keyword, "quick": False, "pg": "1"}).get("list") or []
            report["media_matching_cards"] = len(media_cards)
            for card in media_cards[:3]:
                try:
                    listing = call("detail", {"ids": [card["vod_id"]]})
                    selected = next(iter(listing.get("list") or []), {})
                    if selected.get("vod_play_url"):
                        vod = selected
                        break
                except Exception as exc:
                    if getattr(exc, "status", None) == 429:
                        raise
                    continue
        if vod is None:
            raise ValueError("sample_has_no_playable_detail")
        report["detail_plugin"] = plugin.plugin_id
        groups = list(zip(vod["vod_play_from"].split("$$$"),
                          vod["vod_play_url"].split("$$$")))
        report["play_groups"] = [
            {"index": index, "entries": len(group.split("#")),
             "physical_ids": sum(bool(re.match(r"\d+@", entry.partition("$")[2]))
                                 for entry in group.split("#"))}
            for index, (_, group) in enumerate(groups)
        ]
        physical = [
            (flag, group.split("#"), entry.partition("$")[2], index)
            for index, (flag, group) in enumerate(groups) for entry in group.split("#")
            if re.match(r"\d+@", entry.partition("$")[2])
        ]
        if physical:
            flag, entries, play_id, index = physical[0]
            report["physical_line_index"] = index
        else:
            flag, group = groups[0]
            entries = group.split("#")
            play_id = next(v.partition("$")[2] for v in entries if v.partition("$")[2])
        report["playlist_entries"] = len(entries)
        assets = {}
        for lane, server in (("https-direct", False), ("at-proxy", True)):
            begin = time.perf_counter()
            item = {"operation": lane}
            report["stages"].append(item)
            # Call AT's documented legacy media resolver to retain both network lanes.
            query = native._play_query(plugin, {"id": play_id, "flag": flag,
                                                "_server_proxy": server})
            # This benchmark requests both lanes explicitly, unlike normal logical episodes.
            query["type"] = "t4" if server else "t4-direct"
            try:
                payload = native._request(plugin, native._play_route(plugin), query,
                                          {"X-CLIENT": "t4-sidecar"})
            except Exception as exc:
                item.update(error=type(exc).__name__, status=getattr(exc, "status", None),
                            reason=getattr(exc, "reason", None))
                raise
            url, headers = payload.get("url"), payload.get("header") or {}
            item.update({"elapsed_ms": (time.perf_counter() - begin) * 1000,
                    "header_names": sorted(str(k).lower() for k in headers),
                    "parse": payload.get("parse", 0), "jx": payload.get("jx", 0)})
            if not isinstance(url, str) or urlsplit(url).scheme not in {"https", "http"}:
                item["unsupported"] = "non_http"
                continue
            item.update(classify(url))
            if item["hls"] or item["sidecar_proxy"] or payload.get("parse", 0) not in (0, "0", None):
                item["unsupported"] = "not_raw_media"
                continue
            if lane == "https-direct" and (item["scheme"] != "https" or item["at_proxy"]):
                item["unsupported"] = "direct_https_not_returned"
                continue
            if lane == "at-proxy" and not item["at_proxy"]:
                item["unsupported"] = "at_proxy_not_returned"
                continue
            assets[lane] = {"url": url, "headers": dict(headers)}
        return assets, report
    finally:
        native.close()


@contextmanager
def relays(asset, *, relay_args=(), profile=True):
    children = []
    initial_affinity = psutil.Process().cpu_affinity()
    with tempfile.TemporaryDirectory(prefix="t4-real-", dir="/tmp") as temporary:
        directory = Path(temporary)
        manifest = directory / "assets.json"
        manifest.write_text(json.dumps({"real": asset}), encoding="utf-8")
        manifest.chmod(0o600)
        try:
            python = Child([
                sys.executable, "-X", "utf8", "-m", "tests.rust.relay_support",
                "--serve", "python", "--assets", str(manifest), *(["--profile"] if profile else []),
            ], directory, "python", cpus=[initial_affinity[-1]])
            children.append(python)
            rust = Child([str(binary_path()), "--assets", str(manifest), "--trace", *relay_args],
                         directory, "rust", cpus=[initial_affinity[-1]])
            children.append(rust)
            allowed = psutil.Process().cpu_affinity()
            from scripts.diagnose_rust_affinity import bind_threads
            if len(allowed) > 1:
                bind_threads(os.getpid(), allowed[:-1])
            yield {"python": python, "rust": rust}
        finally:
            for child in reversed(children):
                child.close()
            from scripts.diagnose_rust_affinity import bind_threads
            bind_threads(os.getpid(), initial_affinity)


async def read(client, url, headers, offset, size, *, expected=None, trace=None):
    started = time.perf_counter()
    result = {"offset": offset, "expected_bytes": size, "client_start_us": time.time_ns() // 1000}
    requested = {**headers, "Accept-Encoding": "identity", "Range": f"bytes={offset}-{offset + size - 1}"}
    if trace:
        requested["x-t4-relay-client-trace"] = trace
        result["trace"] = trace
    digest, received = hashlib.sha256(), 0
    try:
        async with client.stream("GET", url, headers=requested) as response:
            result.update(range_metadata(response, offset, size))
            result["headers_ms"] = (time.perf_counter() - started) * 1000
            result["client_headers_us"] = time.time_ns() // 1000
            result["redirects"] = len(response.history)
            retry = response.headers.get("retry-after", "")
            result["retry_after_present"] = bool(retry)
            if retry.isdigit() and len(retry) <= 6:
                result["retry_after_seconds"] = int(retry)
            if not result["range_valid"]:
                result["error"] = "range_contract"
                return result
            async for chunk in response.aiter_raw():
                if "ttfb_ms" not in result:
                    result["ttfb_ms"] = (time.perf_counter() - started) * 1000
                    result["client_first_body_us"] = time.time_ns() // 1000
                received += len(chunk)
                if received > size:
                    result["error"] = "body_limit"
                    break
                digest.update(chunk)
            result["complete"] = received == size and "error" not in result
            result["sha256"] = digest.hexdigest()
            result["hash_match"] = expected is None or result["sha256"] == expected
            if not result["complete"] or not result["hash_match"]:
                result["error"] = "byte_contract"
    except Exception as exc:
        result["error"] = type(exc).__name__
    finally:
        result["bytes"] = received
        result["elapsed_ms"] = (time.perf_counter() - started) * 1000
    return result


def paired_stages(samples, events):
    indexed = {v.get("trace", v.get("client_trace")): v for v in events}
    rows = []
    for sample in samples:
        event = indexed.get(sample.get("trace"))
        if event is None or "client_first_body_us" not in sample:
            continue
        points = {"client": sample["client_start_us"], "handler": event.get("handler_us"),
                  "upstream": event.get("upstream_request_us"),
                  "headers": event.get("upstream_headers_us"),
                  "first": event.get("first_body_us"),
                  "received": sample["client_first_body_us"]}
        stages = {}
        for name, start, end in (
            ("client_to_handler", "client", "handler"),
            ("relay_setup", "handler", "upstream"),
            ("upstream_headers_wait", "upstream", "headers"),
            ("headers_to_first_body", "headers", "first"),
            ("downstream_first_body", "first", "received"),
        ):
            if points[start] is not None and points[end] is not None:
                stages[name] = (points[end] - points[start]) / 1000
        rows.append({"trace": sample["trace"], "stages_ms": stages,
                     "python_exchanges": event.get("upstream_exchanges"),
                     "upstream_chunks": event.get("upstream_chunks"),
                     "max_upstream_chunk_size": event.get("max_upstream_chunk_size")})
    return rows


async def batch(child, client, *, concurrency, size, offset, expected, trace):
    process = psutil.Process(child.process.pid)
    before_memory, before_cpu = memory(process), cpu_seconds(process)
    started = time.perf_counter()
    with MemorySampler(process, enabled=True) as sampler:
        samples = await asyncio.wait_for(asyncio.gather(*(
            read(client, child.url + "/t4/media/real", {}, offset, size, expected=expected,
                 trace=f"{trace}-{index}") for index in range(concurrency)
        )), timeout=80)
    duration = time.perf_counter() - started
    used_cpu = cpu_seconds(process) - before_cpu
    total = sum(v["bytes"] for v in samples)
    latencies = [v["ttfb_ms"] for v in samples if v.get("complete")]
    return {"concurrency": concurrency, "seconds": duration, "bytes": total,
            "passed": all(v.get("complete") and v.get("hash_match") and not v.get("error")
                          for v in samples),
            "mib_per_second": total / MIB / duration, "relay_cpu_seconds": used_cpu,
            "cpu_seconds_per_gib": used_cpu / (total / 1024 ** 3) if total else None,
            "memory_before": before_memory, "memory_after": memory(process),
            "rss_peak": sampler.peak, "samples": samples,
            "ttfb_p95_ms": percentile(latencies, .95) if latencies else None}


async def compare(assets, args, report):
    journal = args.output.with_suffix(".journal.jsonl")
    budget = 0
    deadline = time.monotonic() + 600

    def record(row):
        with journal.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row) + "\n")

    shared_references = {}
    for lane, asset in assets.items():
        lane_report = {"lane": lane, "validation": [], "rows": []}
        report["lanes"].append(lane_report)
        try:
            size = args.window_mib * MIB
            async with httpx.AsyncClient(trust_env=False, follow_redirects=True, timeout=25) as upstream:
                probe = await read(upstream, asset["url"], asset["headers"], 0, 16384)
                lane_report["validation"].append(probe)
                assert probe.get("complete"), "initial_range"
                total = probe["object_bytes"]
                assert total >= size * args.repeats, "sample_too_small"
                offsets = [i * size for i in range(args.repeats)]
                references = {}
                for offset in offsets:
                    row = await read(upstream, asset["url"], asset["headers"], offset, size)
                    lane_report["validation"].append(row)
                    assert row.get("complete"), "reference_range"
                    references[offset] = row["sha256"]
                    budget += row["bytes"]
                seek_references = {}
                for offset in (total // 2, total - 16384):
                    row = await read(upstream, asset["url"], asset["headers"], offset, 16384)
                    lane_report["validation"].append(row)
                    assert row.get("complete"), "seek_range"
                    seek_references[offset] = row["sha256"]
                current_references = {**references, **seek_references}
                if shared_references:
                    lane_report["same_media_across_lanes"] = current_references == shared_references
                    assert lane_report["same_media_across_lanes"], "different_media_across_lanes"
                else:
                    shared_references = current_references
                head = await upstream.head(asset["url"], headers={
                    **asset["headers"], "Accept-Encoding": "identity", "Range": "bytes=0-16383",
                })
                lane_report["head"] = {**range_metadata(head, 0, 16384), "body_bytes": len(head.content)}
            with relays(asset) as instances:
                async with httpx.AsyncClient(trust_env=False, timeout=30,
                    limits=httpx.Limits(max_connections=16, max_keepalive_connections=16)) as client:
                    for mode, child in instances.items():
                        warm = await read(client, child.url + "/t4/media/real", {}, 0, 16384,
                                          expected=probe["sha256"])
                        assert warm.get("hash_match") and warm.get("complete"), "warmup"
                        parity = {"mode": mode, "seek": []}
                        lane_report.setdefault("parity", []).append(parity)
                        local_head = await client.head(child.url + "/t4/media/real",
                                                       headers={"Range": "bytes=0-16383"})
                        parity["head"] = {**range_metadata(local_head, 0, 16384),
                                          "body_bytes": len(local_head.content)}
                        parity["head_matches_upstream"] = parity["head"] == lane_report["head"]
                        assert parity["head_matches_upstream"], "head_parity"
                        for offset, digest in seek_references.items():
                            seek = await read(client, child.url + "/t4/media/real", {},
                                              offset, 16384, expected=digest,
                                              trace=f"{lane}-{mode}-seek-{offset}")
                            parity["seek"].append(seek)
                            assert seek.get("complete") and seek.get("hash_match"), "seek_parity"
                    for concurrency in ([] if args.contracts_only else args.concurrency):
                        for repeat in range(args.repeats):
                            assert budget + 2 * size * concurrency <= 3 * 1024 ** 3, "byte_budget"
                            assert time.monotonic() < deadline, "time_budget"
                            for mode in (("python", "rust") if repeat % 2 == 0 else ("rust", "python")):
                                child = instances[mode]
                                row = await batch(child, client, concurrency=concurrency, size=size,
                                                  offset=offsets[repeat], expected=references[offsets[repeat]],
                                                  trace=f"{lane}-{mode}-{concurrency}-{repeat}")
                                row.update(mode=mode, repeat=repeat, lane=lane)
                                budget += row["bytes"]
                                if mode == "python":
                                    events = (await client.get(child.url + "/__profile")).json()["events"]
                                else:
                                    await asyncio.sleep(.02)
                                    events = []
                                    for line in child.log.read_text(encoding="utf-8").splitlines():
                                        try:
                                            event = json.loads(line)
                                        except ValueError:
                                            continue
                                        if event.get("event") == "relay.closed":
                                            events.append(event)
                                row["paired_stages"] = paired_stages(row["samples"], events)
                                lane_report["rows"].append(row)
                                record(row)
                                emit({k: row[k] for k in (
                                    "lane", "mode", "concurrency", "repeat", "passed",
                                    "mib_per_second", "ttfb_p95_ms", "rss_peak",
                                )})
                                assert row["passed"], "media_batch"
                        # Never escalate traffic after a failed level.
                        if time.monotonic() >= deadline:
                            raise TimeoutError()
                    if args.contracts_only:
                        lane_report["cancel"] = []
                        for mode, child in instances.items():
                            begin = time.perf_counter()
                            async with client.stream("GET", child.url + "/t4/media/real", headers={
                                "Range": f"bytes=0-{size-1}",
                                "x-t4-relay-client-trace": f"{lane}-{mode}-cancel",
                            }) as response:
                                assert response.status_code == 206, "cancel_status"
                                first = await anext(response.aiter_raw())
                            await asyncio.sleep(.5)
                            lane_report["cancel"].append({
                                "mode": mode, "first_chunk_bytes": len(first),
                                "requested_bytes": size,
                                "elapsed_including_settle_ms": (time.perf_counter() - begin) * 1000,
                            })
                    lane_report["rust_health"] = (await client.get(instances["rust"].url + "/health")).json()
                    assert lane_report["rust_health"]["active"] == 0, "active_stream_leaked"
                    await asyncio.sleep(.5)
                    lane_report["idle_memory"] = {
                        mode: memory(psutil.Process(child.process.pid)) for mode, child in instances.items()
                    }
                    lane_report["passed"] = True
        except Exception as exc:
            lane_report.update(passed=False, error=type(exc).__name__)
            record({"lane": lane, "failed": type(exc).__name__})
        finally:
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["measured_bytes"] = budget


def comparisons(lanes, *, repeats=4):
    result = []
    for lane in lanes:
        rows = lane["rows"]
        for concurrency in sorted({r["concurrency"] for r in rows}):
            modes = {mode: [r for r in rows if r["mode"] == mode and r["concurrency"] == concurrency]
                     for mode in ("python", "rust")}
            if any(
                len(group) != repeats or not all(row["passed"] for row in group)
                for group in modes.values()
            ):
                continue
            def median(mode, key):
                values = [r[key] for r in modes[mode] if r.get(key) is not None]
                return statistics.median(values) if values else None
            py_ttfb, rs_ttfb = median("python", "ttfb_p95_ms"), median("rust", "ttfb_p95_ms")
            result.append({
                "lane": lane["lane"], "concurrency": concurrency,
                "python_ttfb_p95_ms": py_ttfb, "rust_ttfb_p95_ms": rs_ttfb,
                "ttfb_p95_delta_ms": rs_ttfb - py_ttfb if py_ttfb and rs_ttfb else None,
                "python_mib_per_second": median("python", "mib_per_second"),
                "rust_mib_per_second": median("rust", "mib_per_second"),
                "throughput_ratio": median("rust", "mib_per_second") / median("python", "mib_per_second"),
                "python_rss_peak": median("python", "rss_peak"), "rust_rss_peak": median("rust", "rss_peak"),
                "python_cpu_seconds_per_gib": median("python", "cpu_seconds_per_gib"),
                "rust_cpu_seconds_per_gib": median("rust", "cpu_seconds_per_gib"),
                "requests_per_mode": {mode: sum(len(r["samples"]) for r in group)
                                      for mode, group in modes.items()},
                "p95_sparse_sample_warning": concurrency * len(modes["python"]) < 100,
            })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resolve-only", action="store_true")
    parser.add_argument("--contracts-only", action="store_true")
    parser.add_argument("--window-mib", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8, 16])
    args = parser.parse_args()
    if not 1 <= args.window_mib <= 8 or not 1 <= args.repeats <= 4 or any(c not in (1, 4, 8, 16) for c in args.concurrency):
        parser.error("bounded windows/repeats/concurrency required")
    logging.disable(logging.CRITICAL)
    report = {"scope": "real-AT-resolved-media-raw-byte-only", "production_ready": False,
              "real_device_verified": False, "full_T4_gateway_verified": False,
              "hls_rewrite_verified": False, "lanes": [], "window_mib": args.window_mib,
              "contracts_only": args.contracts_only,
              "repeats": args.repeats, "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
              "experimental_scheduling": False, "hedge": False}
    stage = "input"
    try:
        # The SSH launcher pipes only the authorized environment; stdin is never logged.
        supplied = json.load(sys.stdin)
        for key in ("ALIST_TVBOX_SUBSCRIPTION_URL", "ALIST_TVBOX_INTERNAL_URL"):
            if supplied.get(key):
                os.environ[key] = supplied[key]
        stage = "resolve"
        report["resolution"] = {}
        assets, _ = resolve(report["resolution"])
        report["resolution"]["usable_lanes"] = list(assets)
        emit({"stage": "resolved", "resolution": report["resolution"], "lanes": list(assets)})
        assert assets, "no_raw_assets"
        if not args.resolve_only:
            stage = "measure"
            asyncio.run(compare(assets, args, report))
            report["comparisons"] = comparisons(report["lanes"], repeats=args.repeats)
            report["passed"] = len(report["lanes"]) == 2 and all(v.get("passed") for v in report["lanes"])
    except Exception as exc:
        report.update(passed=False, failed_stage=stage, error=type(exc).__name__)
    finally:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        emit({"stage": "finished", "passed": report.get("passed"), "error": report.get("error"),
              "comparisons": report.get("comparisons", [])})
    return 0 if (
        args.resolve_only and report.get("resolution", {}).get("usable_lanes")
        or report.get("passed")
    ) else 2


if __name__ == "__main__":
    sys.exit(main())
