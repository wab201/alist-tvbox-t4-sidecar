from __future__ import annotations

import base64
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import itertools
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from lxml import etree

from sidecar.diagnostics import record, trace_id
from sidecar.hls import ClosingStream, HlsError, content_range, png_ts_offset, rewrite_manifest
from sidecar.server_parser import ServerParseError, resolve_page


class PlaybackError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class MediaAsset:
    content: bytes | None
    url: str
    headers: dict[str, str]
    mime: str
    trace: str
    created: float
    kind: str = "binary"
    children: dict[str, MediaAsset] = field(default_factory=dict)
    prefix: int | None = None
    upstream_size: int | None = None


class PlaybackBridge:
    """Keep manifest rewriting, upstream headers and media normalization on the server."""

    def __init__(
        self, *, transport: httpx.BaseTransport | None = None,
        capacity: int = 16384, ttl: float = 7200, proxy_registry=None,
    ) -> None:
        self.client = httpx.Client(
            transport=transport, timeout=httpx.Timeout(25, connect=10),
            follow_redirects=True,
        )
        self._assets: OrderedDict[str, MediaAsset] = OrderedDict()
        self._local_client = httpx.Client(
            transport=transport, trust_env=False, timeout=httpx.Timeout(25, connect=3),
            follow_redirects=True,
        )
        self._proxies = proxy_registry
        self._lock = threading.RLock()
        self._capacity = capacity
        self._size = 0
        self._ttl = ttl

    def close(self) -> None:
        self.client.close()
        self._local_client.close()

    def _evict(self) -> None:
        while self._size > self._capacity:
            _, asset = self._assets.popitem(last=False)
            self._size -= 1 + len(asset.children)

    def _put(self, asset: MediaAsset, suffix: str) -> str:
        key = secrets.token_hex(16) + suffix
        with self._lock:
            self._prune()
            self._assets[key] = asset
            self._size += 1
            self._evict()
        return key

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._ttl
        while self._assets:
            key, asset = next(iter(self._assets.items()))
            if asset.created > cutoff:
                break
            self._assets.pop(key)
            self._size -= 1 + len(asset.children)

    def get(self, key: str) -> MediaAsset | None:
        with self._lock:
            self._prune()
            parent, separator, child = key.partition("~")
            root = self._assets.get(parent)
            asset = root.children.get(child) if root is not None and separator else root
            if asset is not None:
                asset.created = time.monotonic()
                root.created = asset.created
                self._assets.move_to_end(parent)
            return asset

    def _hls_child(self, parent: str, url: str, kind: str, base: str) -> str:
        extension = urlsplit(url).path.lower().rsplit(".", 1)[-1]
        suffix = ".m3u8" if kind == "hls" else "." + extension if extension in {
            "ts", "m4s", "mp4", "aac", "mp3", "webm", "vtt", "srt", "ttml",
        } else ".ts" if kind == "segment" else ".bin"
        child = hashlib.sha256((kind + "\0" + url).encode()).hexdigest()[:32] + suffix
        with self._lock:
            root = self._assets.get(parent)
            if root is None:
                raise PlaybackError("media_session_expired")
            if child not in root.children:
                if len(root.children) >= self._capacity - 1:
                    raise PlaybackError("hls_resource_limit")
                root.children[child] = MediaAsset(
                    None, url, root.headers, "application/octet-stream",
                    root.trace, time.monotonic(), kind,
                )
                self._size += 1
            root.created = time.monotonic()
            self._assets.move_to_end(parent)
            self._evict()
        return base.rstrip("/") + "/t4/media/" + parent + "~" + child

    def _request(self, asset: MediaAsset, method: str, headers: dict) -> httpx.Response:
        url = asset.url
        client = self.client
        resolved = self._proxies.resolve(url) if self._proxies is not None else None
        if resolved is not None:
            url, client = resolved, self._local_client
        elif urlsplit(url).hostname in {"127.0.0.1", "::1", "localhost"}:
            client = self._local_client
        return client.send(client.build_request(method, url, headers=headers), stream=True)

    def render_hls(self, key: str, asset: MediaAsset, base: str) -> bytes:
        headers = {**asset.headers, "Accept-Encoding": "identity"}
        with closing(self._request(asset, "GET", headers)) as response:
            if response.status_code != 200:
                raise PlaybackError("hls_upstream_status")
            chunks, size = [], 0
            started = time.monotonic()
            for chunk in response.iter_bytes(16384):
                size += len(chunk)
                if size > 2_097_152 or time.monotonic() - started > 30:
                    raise PlaybackError("hls_manifest_limit")
                chunks.append(chunk)
            parent = key.partition("~")[0]
            try:
                result = rewrite_manifest(
                    b"".join(chunks), str(response.url),
                    lambda url, kind: self._hls_child(parent, url, kind, base),
                )
            except HlsError as exc:
                raise PlaybackError(str(exc)) from exc
        record("media.hls_manifest", parent_trace=asset.trace, bytes=len(result))
        return result

    def open_media(
        self, asset: MediaAsset, method: str, request_headers: dict,
    ) -> httpx.Response:
        headers = {**asset.headers, "Accept-Encoding": "identity"}
        requested_range = request_headers.get("range", "")
        if_range = request_headers.get("if-range", "")
        if requested_range:
            headers["Range"] = requested_range
        if if_range:
            headers["If-Range"] = if_range
        if asset.kind != "segment":
            return self._request(asset, method, headers)
        try:
            if (requested_range or method == "HEAD") and asset.prefix is None:
                with closing(self._request(asset, "GET", {
                    **asset.headers, "Accept-Encoding": "identity", "Range": "bytes=0-16383",
                })) as probe:
                    if probe.status_code not in {200, 206}:
                        raise PlaybackError("segment_upstream_status")
                    first = next(probe.iter_bytes(16384), b"")
                    asset.prefix = png_ts_offset(first)
                    bounds = content_range(probe.headers)
                    if bounds is not None:
                        asset.upstream_size = bounds[2]
                    elif probe.status_code == 200 and probe.headers.get("content-length", "").isdigit():
                        asset.upstream_size = int(probe.headers["content-length"])
                    record("media.segment_inspected", parent_trace=asset.trace, bytes=asset.prefix)
            offset = asset.prefix or 0
            if offset and requested_range:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested_range)
                if match is None or not (match[1] or match[2]):
                    raise PlaybackError("unsupported_segment_range")
                size = asset.upstream_size - offset if asset.upstream_size is not None else None
                if match[1]:
                    start = int(match[1])
                    if (size is not None and start >= size) or (match[2] and int(match[2]) < start):
                        return httpx.Response(416, headers={
                            "Content-Range": f"bytes */{size}" if size is not None else "bytes */*",
                            "Content-Length": "0",
                        }, stream=httpx.ByteStream(b""))
                    end = str(int(match[2]) + offset) if match[2] else ""
                    headers["Range"] = f"bytes={start + offset}-{end}"
                elif size is not None:
                    count = min(int(match[2]), size)
                    if count == 0:
                        return httpx.Response(416, headers={
                            "Content-Range": f"bytes */{size}", "Content-Length": "0",
                        }, stream=httpx.ByteStream(b""))
                    headers["Range"] = f"bytes={asset.upstream_size - count}-"
                # Without a known length the upstream resolves the suffix range.
            upstream = self._request(asset, method, headers)
            if upstream.status_code not in {200, 206}:
                return upstream
            stream = upstream.iter_bytes(16384)
            first = next(stream, b"") if method != "HEAD" else b""
            if asset.prefix is None:
                asset.prefix = png_ts_offset(first)
                record("media.segment_inspected", parent_trace=asset.trace, bytes=asset.prefix)
            offset = asset.prefix
            bounds = content_range(upstream.headers)
            start = bounds[0] if bounds is not None else 0
            strip = max(0, offset - start) if method != "HEAD" else 0
            response_headers = {
                name: upstream.headers[name] for name in (
                    "content-type", "content-length", "content-range", "accept-ranges",
                ) if name in upstream.headers
            }
            if offset:
                if upstream.status_code == 206 and bounds is None:
                    raise PlaybackError("invalid_media_range")
                if bounds:
                    begin, end, total = bounds
                    if end < offset or total <= offset:
                        raise PlaybackError("invalid_media_range")
                    response_headers["content-range"] = f"bytes {max(begin, offset)-offset}-{end-offset}/{total-offset}"
                length = upstream.headers.get("content-length")
                if length:
                    remove = max(0, offset - start)
                    if int(length) < remove:
                        raise PlaybackError("invalid_media_range")
                    response_headers["content-length"] = str(int(length) - remove)
                response_headers["content-type"] = "video/mp2t"
                record("media.segment_unwrapped", parent_trace=asset.trace, bytes=offset)
            chunks = itertools.chain((first[strip:],), stream) if method != "HEAD" else iter(())
            return httpx.Response(
                upstream.status_code, headers=response_headers,
                stream=ClosingStream(chunks, upstream.close),
            )
        except (HlsError, ValueError) as exc:
            if "upstream" in locals():
                upstream.close()
            raise PlaybackError(str(exc)) from exc
        except BaseException:
            if "upstream" in locals():
                upstream.close()
            raise

    def prepare(self, payload: dict[str, Any], base: str) -> dict[str, Any]:
        output = dict(payload)
        url = output.get("url")
        record(
            "playback.result", scheme=urlsplit(url).scheme if isinstance(url, str) else "other",
            format=output.get("format", "unknown"),
            parse=output.get("parse", 0), jx=output.get("jx", 0),
            header_count=len(output.get("header") or {}),
        )
        parsed_by_server = output.get("parse") not in {None, 0, "0"} or output.get("jx") not in {None, 0, "0"}
        if parsed_by_server:
            if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
                raise PlaybackError("server_parser_non_http")
            try:
                def request_page(target, headers):
                    return closing(self._request(MediaAsset(
                        None, target, {}, "text/html", trace_id(), time.monotonic(),
                    ), "GET", {**headers, "Accept-Encoding": "identity"}))
                headers = output.get("header") or {}
                if not isinstance(headers, dict):
                    raise PlaybackError("invalid_play_headers")
                url, headers, media_format = resolve_page(url, headers, request_page)
            except ServerParseError as exc:
                record("playback.server_parser_failed", reason=str(exc))
                raise PlaybackError(str(exc)) from exc
            except httpx.HTTPError as exc:
                record("playback.server_parser_failed", reason="server_parser_request_failed")
                raise PlaybackError("server_parser_request_failed") from exc
            output.update(url=url, header=headers, parse=0, jx=0, playUrl="")
            if media_format:
                output["format"] = media_format
            record("playback.server_parser_complete")
        if isinstance(url, str) and urlsplit(url).scheme in {"http", "https"} and (
            urlsplit(url).path.lower().endswith(".m3u8")
            or str(output.get("format", "")).split(";")[0] in {
                "application/vnd.apple.mpegurl", "application/x-mpegurl",
            }
        ) and output.get("parse") in {None, 0, "0"} and output.get("jx") in {None, 0, "0"}:
            if urlsplit(url).path.startswith("/t4/media/") and self.get(urlsplit(url).path.rsplit("/", 1)[-1]):
                return output
            headers = output.get("header") or {}
            if not isinstance(headers, dict):
                raise PlaybackError("invalid_play_headers")
            key = self._put(MediaAsset(
                None, url, dict(headers), "application/vnd.apple.mpegurl",
                trace_id(), time.monotonic(), "hls",
            ), ".m3u8")
            output.update(url=base.rstrip("/") + "/t4/media/" + key, header={}, parse=0, jx=0,
                          playUrl="", format="application/vnd.apple.mpegurl")
            record("playback.hls_http", asset=key)
            return output
        if not isinstance(url, str) or not url.startswith("data:application/dash+xml"):
            # Ordinary media and its required headers belong to the player.
            # A URL's extension is not evidence of its container or codecs.
            record("playback.direct_http", header_count=len(output.get("header") or {}))
            return output
        try:
            prefix, encoded = url.split(",", 1)
            if ";base64" not in prefix or len(encoded) > 1_400_000:
                raise ValueError("invalid inline DASH")
            raw = base64.b64decode("".join(encoded.split()), validate=True)
            root = etree.fromstring(
                raw, etree.XMLParser(resolve_entities=False, no_network=True)
            )
            if etree.QName(root).localname != "MPD" or root.getroottree().docinfo.doctype:
                raise ValueError("invalid MPD")
            headers = output.get("header") or {}
            if not isinstance(headers, dict):
                raise ValueError("invalid headers")
            nodes = root.findall(".//{*}BaseURL")
            if not nodes or len(nodes) > 128:
                raise ValueError("missing DASH assets")
            # AT uses SegmentBase representations; do not guess template URL semantics.
            if root.findall(".//{*}SegmentTemplate") or root.findall(".//{*}SegmentList"):
                raise ValueError("unsupported inline DASH segmentation")
            for node in nodes:
                target = (node.text or "").strip()
                if urlsplit(target).scheme not in {"http", "https"}:
                    raise ValueError("DASH media URL must be absolute HTTP")
            parent = trace_id()
            now = time.monotonic()
            for node in nodes:
                key = self._put(MediaAsset(
                    None, node.text.strip(), dict(headers),
                    "application/octet-stream", parent, now,
                ), ".mp4")
                node.text = base.rstrip("/") + "/t4/media/" + key
            manifest = etree.tostring(root, encoding="utf-8", xml_declaration=True)
            key = self._put(MediaAsset(
                manifest, "", {}, "application/dash+xml", parent, now,
            ), ".mpd")
        except (ValueError, TypeError, etree.XMLSyntaxError) as exc:
            record("playback.rejected", error=type(exc).__name__)
            raise PlaybackError("AT returned an invalid or unsupported inline DASH manifest") from exc
        output.update(
            url=base.rstrip("/") + "/t4/media/" + key,
            format="application/dash+xml", parse=0, jx=0, header={},
        )
        record("playback.dash_http", count=len(nodes), bytes=len(manifest), asset=key)
        return output

    def probe(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Bounded byte probe, not a claim of real-device continuous playback."""
        url = payload.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
            record("probe.unsupported", reason="non_http_media")
            return {"ok": False, "stage": "media", "reason": "non_http_media"}
        key = urlsplit(url).path.rsplit("/", 1)[-1]
        manifest = self.get(key) if "/t4/media/" in urlsplit(url).path else None
        targets = []
        if manifest is not None and manifest.content is not None:
            root = etree.fromstring(manifest.content)
            for content_type in ("video/", "audio/"):
                representation = next((
                    r for r in root.findall(".//{*}Representation")
                    if (r.get("mimeType") or "").startswith(content_type)
                ), None)
                if representation is not None:
                    node = representation.find("{*}BaseURL")
                    asset = self.get(node.text.rsplit("/", 1)[-1]) if node is not None else None
                    if asset is not None:
                        targets.append((content_type[:-1], asset.url, asset.headers))
        else:
            targets.append(("media", url, payload.get("header") or {}))
        results = []
        for kind, target, headers in targets:
            start = time.monotonic()
            try:
                with self.client.stream(
                    "GET", target, headers={**headers, "Range": "bytes=0-4095"}
                ) as response:
                    chunk = next(response.iter_bytes(chunk_size=4096), b"")
                    media_type = response.headers.get("content-type", "").split(";", 1)[0]
                    recognized = (
                        chunk[4:8] in {b"ftyp", b"styp", b"moof", b"moov"}
                        or chunk.startswith((b"#EXTM3U", b"\x47", b"\x1a\x45\xdf\xa3", b"FLV"))
                        or b"<MPD" in chunk[:1024]
                    )
                    manifest_only = chunk.startswith(b"#EXTM3U") or b"<MPD" in chunk[:1024]
                    item = {
                        "kind": kind, "status": response.status_code,
                        "bytes": len(chunk), "recognized": recognized,
                        "ok": response.status_code in {200, 206} and recognized and not manifest_only,
                        "content_type": media_type,
                    }
                    if manifest_only:
                        item["reason"] = "manifest_only"
                    record("probe.media", status=response.status_code, bytes=len(chunk),
                           format=media_type, reason=kind)
            except httpx.HTTPError as exc:
                item = {"kind": kind, "ok": False, "error": type(exc).__name__}
                record("probe.failed", error=type(exc).__name__, reason=kind)
            item["duration_ms"] = round((time.monotonic() - start) * 1000)
            results.append(item)
        return {
            "ok": bool(results) and all(item["ok"] for item in results),
            "stage": "media_bytes", "tracks": results, "real_device_verified": False,
            **({"reason": "manifest_only"} if any(
                item.get("reason") == "manifest_only" for item in results
            ) else {}),
        }
