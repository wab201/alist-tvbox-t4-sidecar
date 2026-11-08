"""Low-frequency real-link diagnosis. Reports retain no URL, cookie or header values."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import hashlib
import json
import logging
import os
from pathlib import Path
import statistics
import sys
import time
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from scripts.benchmark_rust_real_media import emit, paired_stages, read, relays, resolve
from tests.rust.relay_support import binary_path

TRANSPORT_EVENTS = frozenset(
    f"{prefix}.{phase}"
    for prefix in (
        "connection.connect_tcp", "connection.start_tls",
        "http11.send_request_headers", "http11.receive_response_headers",
        "http11.receive_response_body",
    )
    for phase in ("started", "complete", "failed")
)


def safe_failure(response):
    """Classify only fixed known messages, never copy upstream text into reports."""
    result = {"status": response.status_code}
    if response.status_code < 400:
        return result
    # Catalog errors are small JSON responses, not media. Cap before decoding.
    if len(response.content) > 65536:
        result["oversized_error"] = True
        return result
    try:
        payload = response.json()
    except ValueError:
        return result
    if not isinstance(payload, dict):
        return result
    message = str(payload.get("detail") or payload.get("message") or "")
    result["markers"] = [
        label for marker, label in (
            ("找不到文件", "file_missing"), ("不存在", "not_found"),
            ("Not found", "not_found_en"), ("For input string", "number_format"),
            ("Invalid share link format", "invalid_share"),
            ("NumberFormatException", "number_format_exception"),
            ("站点", "site"), ("无权", "forbidden"), ("失效", "expired"),
            ("加载文件失败", "load_file_failed"), ("Bad Request", "generic_bad_request"),
            ("failed get", "upstream_get_failed"), ("not found", "not_found_lower"),
            ("no such file", "no_such_file"), ("storage", "storage"),
            ("失败", "failure"),
            ("too many", "throttled"), ("Too Many Requests", "throttled_en"),
        ) if marker in message
    ]
    result["has_message"] = bool(message)
    words = set(message.lower().split())
    result["known_words"] = sorted(words.intersection({
        "failed", "get", "objs", "list", "file", "not", "found", "storage",
        "mount", "path", "disabled", "sign", "expired", "no", "such", "directory",
        "bad", "request", "invalid", "permission", "denied", "token", "missing",
    }))
    for key in ("code", "status"):
        if type(payload.get(key)) is int:
            result["json_" + key] = payload[key]
    return result


def transport_summary(exchanges):
    rows = []
    for exchange in exchanges:
        events = exchange["transport_us"]
        stages = {}
        for name, prefix in (
            ("connect_tcp", "connection.connect_tcp"),
            ("tls", "connection.start_tls"),
            ("send_headers", "http11.send_request_headers"),
            ("receive_headers", "http11.receive_response_headers"),
        ):
            start, end = events.get(prefix + ".started"), events.get(prefix + ".complete")
            if start is not None and end is not None:
                stages[name] = (end - start) / 1000
        rows.append({
            "status": exchange.get("status"), "http_version": exchange.get("http_version"),
            "connection_close": exchange.get("connection_close"),
            # Absence of connect events is evidence consistent with reuse, not a socket ID.
            "new_connection_observed": "connection.connect_tcp.started" in events,
            "stages_ms": stages,
        })
    return rows


def internal_asset(asset, subscription, internal):
    """Only rewrite the subscribed origin's /p/ route to the configured internal origin."""
    target, public, local = map(urlsplit, (asset["url"], subscription, internal))
    def origin(parsed):
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    if (origin(target) != origin(public) or not target.path.startswith("/p/")
            or local.scheme not in {"http", "https"} or not local.hostname
            or local.username or local.password):
        return None
    return {**asset, "url": urlunsplit((
        local.scheme, local.netloc, local.path.rstrip("/") + target.path,
        target.query, target.fragment,
    ))}


async def measured_upstream(client, asset, size):
    exchanges = []
    async def on_request(request):
        row = {"transport_us": {}, "started_us": time.time_ns() // 1000}
        exchanges.append(row)
        async def trace(name, info):
            if name in TRANSPORT_EVENTS:
                row["transport_us"][name] = time.time_ns() // 1000
        request.extensions["trace"] = trace
    async def on_response(response):
        exchanges[-1].update(
            status=response.status_code, http_version=response.http_version,
            connection_close="close" in [
                v.strip().lower() for v in response.headers.get("connection", "").split(",")
            ],
        )
    client.event_hooks = {"request": [on_request], "response": [on_response]}
    sample = await read(client, asset["url"], asset["headers"], 0, size)
    sample["exchanges"] = transport_summary(exchanges)
    return sample


async def diagnose(assets, args, report):
    proxy = assets.get("at-proxy")
    if proxy and (internal := os.environ.get("ALIST_TVBOX_INTERNAL_URL")):
        candidate = internal_asset(proxy, os.environ["ALIST_TVBOX_SUBSCRIPTION_URL"], internal)
        if candidate:
            assets = {**assets, "at-internal": candidate}
    report["internal_lane_available"] = "at-internal" in assets
    if args.lanes:
        assets = {key: value for key, value in assets.items() if key in args.lanes}
        if set(assets) != set(args.lanes):
            raise ValueError("requested_lane_unavailable")
    reference = None
    size = 128 * 1024
    for lane, asset in assets.items():
        lane_report = {"lane": lane, "origin": [], "relays": []}
        report["lanes"].append(lane_report)
        with (nullcontext({}) if args.origin_only else relays(asset)) as instances:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=True, timeout=25,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            ) as upstream, httpx.AsyncClient(trust_env=False, timeout=25) as client:
                for repeat in range(args.repeats):
                    # A fresh first request followed by fully consumed same-client requests.
                    row = await measured_upstream(upstream, asset, size)
                    row["repeat"] = repeat
                    lane_report["origin"].append(row)
                    if row.get("status") == 429:
                        report["stopped_for_throttle"] = True
                        return
                    if not row.get("complete"):
                        report["stopped_for_contract"] = True
                        return
                    if reference is None:
                        reference = row["sha256"]
                    if row["sha256"] != reference:
                        report["stopped_for_media_mismatch"] = True
                        return
                    await asyncio.sleep(args.interval)
                    for mode in ([] if args.origin_only else
                                 (("python", "rust") if repeat % 2 == 0 else ("rust", "python"))):
                        child = instances[mode]
                        sample = await read(
                            client, child.url + "/t4/media/real", {}, 0, size,
                            expected=reference, trace=f"diag-{lane}-{mode}-{repeat}",
                        )
                        sample.update(mode=mode, repeat=repeat)
                        if mode == "python":
                            events = (await client.get(child.url + "/__profile")).json()["events"]
                        else:
                            await asyncio.sleep(.03)
                            events = []
                            for line in child.log.read_text(encoding="utf-8").splitlines():
                                try:
                                    event = json.loads(line)
                                except ValueError:
                                    continue
                                if event.get("event") == "relay.closed":
                                    events.append(event)
                        sample["paired_stages"] = paired_stages([sample], events)
                        if mode == "python":
                            event = next((e for e in events if e.get("trace") == sample["trace"]), {})
                            sample["exchanges"] = transport_summary(event.get("upstream_exchanges", []))
                        lane_report["relays"].append(sample)
                        emit({"lane": lane, "mode": mode, "repeat": repeat,
                              "status": sample.get("status"), "ttfb_ms": sample.get("ttfb_ms")})
                        if sample.get("status") == 429:
                            report["stopped_for_throttle"] = True
                            return
                        if not sample.get("complete") or not sample.get("hash_match"):
                            report["stopped_for_contract"] = True
                            return
                        await asyncio.sleep(args.interval)
                if not args.origin_only:
                    lane_report["rust_health"] = (await client.get(instances["rust"].url + "/health")).json()
                lane_report["passed"] = args.origin_only or lane_report["rust_health"]["active"] == 0
    report["passed"] = bool(report["lanes"]) and all(v.get("passed") for v in report["lanes"])
    report["measured_bytes"] = sum(
        row["bytes"] for lane in report["lanes"]
        for row in lane["origin"] + lane["relays"]
    )
    report["summary"] = [
        {"lane": lane["lane"],
         "origin_median_ttfb_ms": statistics.median([r["ttfb_ms"] for r in lane["origin"]]),
         **{
            mode + "_median_ttfb_ms": statistics.median(values) if (values := [
                row["ttfb_ms"] for row in lane["relays"] if row["mode"] == mode
            ]) else None for mode in ("python", "rust")
        }} for lane in report["lanes"]
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, choices=(2, 3, 4), default=3)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--origin-only", action="store_true")
    parser.add_argument("--lanes", nargs="+", choices=("https-direct", "at-proxy", "at-internal"))
    args = parser.parse_args()
    if not 1 <= args.interval <= 10:
        parser.error("one to ten seconds between requests")
    logging.disable(logging.CRITICAL)
    report = {"scope": "single-stream-low-frequency-real-diagnosis", "lanes": [],
              "passed": False, "production_ready": False, "real_device_verified": False,
              "binary_sha256": hashlib.sha256(binary_path().read_bytes()).hexdigest(),
              "catalog_errors": [], "origin_only": args.origin_only}
    try:
        supplied = json.load(sys.stdin)
        for key in ("ALIST_TVBOX_SUBSCRIPTION_URL", "ALIST_TVBOX_INTERNAL_URL"):
            if supplied.get(key):
                os.environ[key] = supplied[key]
        def observer(response):
            if response.status_code >= 400:
                response.read()
                report["catalog_errors"].append(safe_failure(response))
        report["resolution"] = {}
        assets, _ = resolve(report["resolution"], response_observer=observer)
        assert len(assets) == 2, "both_lanes_required"
        asyncio.run(diagnose(assets, args, report))
    except Exception as exc:
        report["error"] = type(exc).__name__
    finally:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        emit({"passed": report["passed"], "catalog_errors": report["catalog_errors"],
              "summary": report.get("summary"), "error": report.get("error"),
              "stopped_for_throttle": report.get("stopped_for_throttle", False)})
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
