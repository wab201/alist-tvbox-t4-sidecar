from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from lxml import etree

from sidecar.diagnostics import record, trace_id


class PlaybackError(ValueError):
    pass


@dataclass
class MediaAsset:
    content: bytes | None
    url: str
    headers: dict[str, str]
    mime: str
    trace: str
    created: float


class PlaybackBridge:
    """Expose inline DASH over HTTP, with range forwarding for both audio and video."""

    def __init__(
        self, *, transport: httpx.BaseTransport | None = None,
        capacity: int = 512, ttl: float = 7200,
    ) -> None:
        self.client = httpx.Client(
            transport=transport, timeout=httpx.Timeout(25, connect=10),
            follow_redirects=True,
        )
        self._assets: OrderedDict[str, MediaAsset] = OrderedDict()
        self._lock = threading.Lock()
        self._capacity = capacity
        self._ttl = ttl

    def close(self) -> None:
        self.client.close()

    def _put(self, asset: MediaAsset, suffix: str) -> str:
        key = secrets.token_hex(16) + suffix
        with self._lock:
            self._prune()
            self._assets[key] = asset
            while len(self._assets) > self._capacity:
                self._assets.popitem(last=False)
        return key

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._ttl
        while self._assets:
            key, asset = next(iter(self._assets.items()))
            if asset.created > cutoff:
                break
            self._assets.pop(key)

    def get(self, key: str) -> MediaAsset | None:
        with self._lock:
            self._prune()
            asset = self._assets.get(key)
            if asset is not None:
                asset.created = time.monotonic()
                self._assets.move_to_end(key)
            return asset

    def prepare(self, payload: dict[str, Any], base: str) -> dict[str, Any]:
        output = dict(payload)
        url = output.get("url")
        record(
            "playback.result", scheme=urlsplit(url).scheme if isinstance(url, str) else "other",
            format=output.get("format", "unknown"),
            parse=output.get("parse", 0), jx=output.get("jx", 0),
            header_count=len(output.get("header") or {}),
        )
        if not isinstance(url, str) or not url.startswith("data:application/dash+xml"):
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
