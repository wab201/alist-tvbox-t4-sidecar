from __future__ import annotations

import json
import io
import re
import time
import warnings
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from Crypto.Cipher import AES
from lxml import etree
import m3u8
from PIL import Image, UnidentifiedImageError

from sidecar.playback import PlaybackBridge


def image_kind(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return "avif"
    if data.startswith(b"BM"):
        return "bmp"
    return ""


def media_kind(data: bytes) -> str:
    if data[4:8] in {b"ftyp", b"styp", b"moof", b"moov"}:
        return "mp4"
    if _mpegts_prefix(data):
        return "mpegts"
    if _adts_prefix(data):
        return "aac"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if data.startswith(b"FLV"):
        return "flv"
    return ""


def _mpegts_prefix(data: bytes) -> bool:
    if len(data) >= 565 and all(data[n] == 0x47 for n in (0, 188, 376, 564)):
        return True
    # HLS timed metadata may precede transport-stream packets. Only accept a
    # valid ID3 synchsafe length followed by aligned TS packet sync bytes.
    if not data.startswith(b"ID3") or len(data) < 10:
        return False
    size = 0
    for value in data[6:10]:
        if value & 0x80:
            return False
        size = (size << 7) | value
    offset = 10 + size
    return offset >= 10 and len(data) >= offset + 565 and all(
        data[offset + n] == 0x47 for n in (0, 188, 376, 564)
    )


def _adts_prefix(data: bytes) -> bool:
    offset = 0
    for _ in range(2):
        if len(data) < offset + 7:
            return False
        head = data[offset:offset + 7]
        if head[0] != 0xFF or head[1] & 0xF6 != 0xF0 or (head[2] >> 2) & 0xF == 0xF:
            return False
        length = ((head[3] & 3) << 11) | (head[4] << 3) | (head[5] >> 5)
        if length < (7 if head[1] & 1 else 9):
            return False
        offset += length
    return offset <= len(data)


def _hls_kind_allowed(kind: str, *, audio: bool) -> bool:
    return kind in ({"aac", "mpegts", "mp4"} if audio else {"mpegts", "mp4", "webm"})


class ProbeFailure(Exception):
    def __init__(self, reason: str, *, status: str = "fail", **evidence: Any):
        self.result = {"status": status, "reason": reason, **evidence}


class ResourceProbes:
    """Bounded downloads; raw URLs, headers and content never enter a report."""

    def __init__(self, bridge: PlaybackBridge, transport=None, *, proxy_registry=None) -> None:
        self.bridge = bridge
        self.proxy_registry = proxy_registry
        self._local_client = httpx.Client(
            trust_env=False, timeout=httpx.Timeout(8, connect=4), follow_redirects=True,
            transport=transport,
        )
        self.client = httpx.Client(
            transport=transport, follow_redirects=True,
            timeout=httpx.Timeout(8, connect=4),
        )

    def close(self) -> None:
        self.client.close()
        self._local_client.close()

    def _fetch(self, url: str, headers=None, *, limit=262144, start=0):
        client = self.client
        if self.proxy_registry is not None:
            resolved = self.proxy_registry.resolve(url)
            if resolved:
                url, client = resolved, self._local_client
        if urlsplit(url).hostname in {"127.0.0.1", "::1", "localhost"}:
            client = self._local_client
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ProbeFailure("non_http_resource", status="unsupported")
        # Inline DASH assets are already the exact public bridge output.
        path = urlsplit(url).path
        asset = self.bridge.get(path.rsplit("/", 1)[-1]) if path.startswith("/t4/media/") else None
        if asset is not None:
            if asset.content is not None:
                return asset.content[:limit], {"content-type": asset.mime}, url, 200
            return self._fetch(asset.url, asset.headers, limit=limit, start=start)
        try:
            started = time.monotonic()
            with client.stream("GET", url, headers={
                **(headers or {}), "Range": f"bytes={start}-{start + limit - 1}",
                "Accept-Encoding": "identity",
            }) as response:
                if response.status_code not in {200, 206}:
                    raise ProbeFailure("http_status", http_status=response.status_code)
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=min(limit, 16384)):
                    if time.monotonic() - started > 12:
                        raise ProbeFailure("resource_deadline", status="blocked")
                    data.extend(chunk[:limit - len(data)])
                    if len(data) >= limit:
                        break
                return bytes(data), dict(response.headers), str(response.url), response.status_code
        except httpx.HTTPError as exc:
            raise ProbeFailure("network_error", status="blocked", error=type(exc).__name__) from exc

    def image(self, value: Any) -> dict[str, Any]:
        try:
            if not isinstance(value, str) or not value:
                raise ProbeFailure("missing_image")
            headers = {}
            url = value
            if "@{" in value:
                url, raw = value.split("@", 1)
                headers = json.loads(raw)
                if not isinstance(headers, dict):
                    raise ProbeFailure("invalid_image_headers")
            elif "@" in value:
                url, *parts = value.split("@")
                for part in parts:
                    key, separator, val = part.partition("=")
                    if not separator:
                        raise ProbeFailure("image_header_syntax", status="unsupported")
                    headers[key] = val
            limit = 2_097_152
            data, response_headers, _, status = self._fetch(url, headers, limit=limit)
            kind = image_kind(data)
            mime = response_headers.get("content-type", "").split(";")[0].lower()
            expected = {
                "png": {"image/png"}, "jpeg": {"image/jpeg", "image/jpg", "image/pjpeg"},
                "gif": {"image/gif"}, "webp": {"image/webp"}, "avif": {"image/avif"},
                "bmp": {"image/bmp", "image/x-ms-bmp"},
            }
            if not kind:
                raise ProbeFailure("not_an_image", http_status=status, bytes=len(data))
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(data)) as image:
                        if image.width * image.height > 16_000_000:
                            raise ProbeFailure("image_dimensions_limit", status="blocked")
                        image.load()
                        width, height = image.size
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
                    Image.DecompressionBombWarning) as exc:
                raise ProbeFailure(
                    "image_probe_limit" if len(data) == limit else "invalid_image_bytes",
                    status="blocked" if len(data) == limit else "fail",
                ) from exc
            return {
                "status": "pass", "format": kind, "http_status": status, "bytes": len(data),
                "width": width, "height": height, "evidence_level": "decoded_image",
                "mime_matches": mime in expected[kind],
                **({"warning": "mime_mismatch"} if mime not in expected[kind] else {}),
            }
        except ProbeFailure as exc:
            return exc.result
        except (ValueError, TypeError):
            return {"status": "fail", "reason": "invalid_image"}

    def media(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if payload.get("parse") not in {None, 0, "0"} or payload.get("jx") not in {None, 0, "0"}:
                raise ProbeFailure("client_parser_required", status="blocked")
            url = payload.get("url")
            if not isinstance(url, str) or not url:
                raise ProbeFailure("missing_play_url")
            headers = payload.get("header") or {}
            if not isinstance(headers, dict):
                raise ProbeFailure("invalid_play_headers")
            data, rh, final, status = self._fetch(url, headers)
            if data.lstrip().startswith(b"#EXTM3U"):
                return self._hls(data, final, headers)
            if b"<MPD" in data[:2048]:
                return self._dash(data, final, headers)
            kind = media_kind(data)
            if not kind:
                raise ProbeFailure("not_media_bytes", http_status=status, bytes=len(data))
            samples = [{"position": "start", "http_status": status, "bytes": len(data)}]
            if kind in {"mp4", "webm"}:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", rh.get("content-range", ""))
                if status != 206 or match is None or int(match[1]) != 0:
                    raise ProbeFailure("range_unverified", status="blocked", format=kind)
                total = int(match[3])
                if int(match[2]) != len(data) - 1:
                    raise ProbeFailure("invalid_content_range")
                for label, offset in (("middle", total // 2), ("tail", max(0, total - 4096))):
                    chunk, range_headers, _, code = self._fetch(url, headers, limit=4096, start=offset)
                    expected = f"bytes {offset}-{min(total - 1, offset + 4095)}/{total}"
                    if code != 206 or range_headers.get("content-range") != expected or len(chunk) != min(4096, total - offset):
                        raise ProbeFailure("range_inconsistent", position=label)
                    samples.append({"position": label, "http_status": code, "bytes": len(chunk)})
            return {"status": "pass", "format": kind, "samples": samples,
                    "evidence_level": "media_reachable", "real_device_verified": False}
        except ProbeFailure as exc:
            return {**exc.result, "real_device_verified": False}
        except (ValueError, TypeError, etree.XMLSyntaxError):
            return {"status": "fail", "reason": "invalid_media_contract", "real_device_verified": False}

    def _hls(self, data: bytes, url: str, headers: dict) -> dict[str, Any]:
        playlist = m3u8.loads(data.decode("utf-8-sig"), uri=url)
        master = playlist.is_variant
        variants = len(playlist.playlists) if master else 1
        keys = {}
        if master:
            if not playlist.playlists:
                raise ProbeFailure("empty_master")
            video = playlist.playlists[0]
            video_data, _, video_url, _ = self._fetch(video.absolute_uri, headers)
            if not video_data.lstrip().startswith(b"#EXTM3U"):
                raise ProbeFailure("invalid_variant")
            video_result = self._probe_hls_playlist(
                m3u8.loads(video_data.decode("utf-8-sig"), uri=video_url),
                headers, keys, audio=False,
            )
            audio_group = video.stream_info.audio
            audio = next((
                item for item in playlist.media
                if item.type == "AUDIO" and item.group_id == audio_group and item.uri
                and str(getattr(item, "default", "")).upper() in {"YES", "TRUE", "1"}
            ), None)
            if audio is None and audio_group:
                audio = next((
                    item for item in playlist.media
                    if item.type == "AUDIO" and item.group_id == audio_group and item.uri
                ), None)
            if audio is not None:
                audio_data, _, audio_url, _ = self._fetch(audio.absolute_uri, headers)
                if not audio_data.lstrip().startswith(b"#EXTM3U"):
                    raise ProbeFailure("invalid_external_audio_playlist", track="audio")
                audio_result = self._probe_hls_playlist(
                    m3u8.loads(audio_data.decode("utf-8-sig"), uri=audio_url),
                    headers, keys, audio=True,
                )
                video_result["audio"] = {
                    "selection": "default" if audio.default == "YES" else "first",
                    "segments": audio_result["segments"],
                }
            video_result.update(master=True, variant_count=1, published_variants=variants)
            return video_result
        return self._probe_hls_playlist(playlist, headers, keys, audio=False)

    def _probe_hls_playlist(
        self, playlist, headers: dict, keys: dict, *, audio: bool,
    ) -> dict[str, Any]:
        if playlist.is_variant or not playlist.segments:
            raise ProbeFailure("empty_media_playlist", track="audio" if audio else "video")
        samples = []
        offset = 0
        previous_uri = None
        for index, segment in enumerate(playlist.segments[:2]):
            encrypted = segment.key is not None and segment.key.method not in {None, "NONE"}
            if encrypted and (
                segment.key.method != "AES-128" or segment.key.keyformat not in {None, "identity"}
            ):
                raise ProbeFailure("protected_hls_not_probed", status="blocked")
            start, limit = 0, 32768
            if segment.byterange:
                length, _, position = segment.byterange.partition("@")
                if not position and (previous_uri is None or segment.absolute_uri != previous_uri):
                    raise ProbeFailure("invalid_implicit_byterange")
                start = int(position) if position else offset
                if int(length) <= 0 or start < 0:
                    raise ProbeFailure("invalid_segment_byterange")
                limit = min(int(length), 32768)
                offset = start + int(length)
                previous_uri = segment.absolute_uri
            chunk, rh, _, code = self._fetch(segment.absolute_uri, headers, start=start, limit=limit)
            if segment.byterange:
                if code != 206 or not rh.get("content-range", "").startswith(f"bytes {start}-"):
                    raise ProbeFailure("segment_range_inconsistent")
            if encrypted:
                if segment.byterange:
                    raise ProbeFailure("encrypted_hls_byterange_not_probed", status="blocked")
                chunk = self._decrypt_hls(
                    chunk, segment.key, playlist.media_sequence + index, headers, keys,
                )
            if segment.init_section:
                init, _, _, _ = self._fetch(segment.init_section.absolute_uri, headers, limit=4096)
                if encrypted:
                    if not segment.key.iv:
                        raise ProbeFailure("encrypted_hls_init_requires_iv")
                    init = self._decrypt_hls(init, segment.key, None, headers, keys)
                if media_kind(init) != "mp4":
                    raise ProbeFailure("invalid_init_bytes")
            kind = media_kind(chunk)
            if not _hls_kind_allowed(kind, audio=audio):
                raise ProbeFailure(
                    "invalid_audio_segment_bytes" if audio else "invalid_segment_bytes",
                    segment=index + 1, http_status=code,
                )
            samples.append({"segment": index + 1, "http_status": code,
                            "bytes": len(chunk), "format": kind,
                            **({"encryption": "AES-128", "decrypted_prefix": True} if encrypted else {})})
        return {"status": "pass", "format": "hls",
                "segments": samples, "evidence_level": "media_reachable",
                "continuous_play_verified": False, "real_device_verified": False}

    def _decrypt_hls(self, data, key, sequence, headers, keys):
        if key.method != "AES-128" or key.keyformat not in {None, "identity"}:
            raise ProbeFailure("protected_hls_not_probed", status="blocked")
        if not key.uri:
            raise ProbeFailure("missing_hls_key")
        if key.absolute_uri not in keys:
            raw, _, _, _ = self._fetch(key.absolute_uri, headers, limit=17)
            if len(raw) != 16:
                raise ProbeFailure("invalid_hls_key_length")
            keys[key.absolute_uri] = raw
        if key.iv:
            if not re.fullmatch(r"0[xX][0-9a-fA-F]{1,32}", key.iv):
                raise ProbeFailure("invalid_hls_iv")
            iv = int(key.iv, 16).to_bytes(16, "big")
        elif sequence is not None and 0 <= sequence < 2**128:
            iv = sequence.to_bytes(16, "big")
        else:
            raise ProbeFailure("invalid_hls_media_sequence")
        size = len(data) - len(data) % AES.block_size
        if size < AES.block_size:
            raise ProbeFailure("invalid_encrypted_segment")
        # This is a bounded startup-prefix check, not full-segment PKCS7/decoder verification.
        return AES.new(keys[key.absolute_uri], AES.MODE_CBC, iv).decrypt(data[:size])

    def _dash(self, data: bytes, url: str, headers: dict) -> dict[str, Any]:
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
        if etree.QName(root).localname != "MPD" or root.getroottree().docinfo.doctype:
            raise ProbeFailure("invalid_mpd")
        if root.findall(".//{*}SegmentTemplate") or root.findall(".//{*}SegmentList"):
            raise ProbeFailure("dash_segmentation_not_probed", status="unsupported")
        if root.findall(".//{*}ContentProtection"):
            raise ProbeFailure("protected_dash", status="blocked")
        tracks = []
        for track in ("video", "audio"):
            reps = [
                rep for rep in root.findall(".//{*}Representation")
                if (rep.get("mimeType") or rep.getparent().get("mimeType") or "").startswith(track + "/")
                or rep.getparent().get("contentType") == track
            ]
            if not reps:
                continue
            node = reps[0].find("{*}BaseURL")
            if node is None or not node.text:
                raise ProbeFailure("dash_baseurl_not_probed", status="unsupported")
            chunk, _, _, code = self._fetch(urljoin(url, node.text), headers, limit=4096)
            if media_kind(chunk) != "mp4":
                raise ProbeFailure("invalid_dash_track", track=track)
            tracks.append({"kind": track, "http_status": code, "bytes": len(chunk)})
        if not tracks:
            raise ProbeFailure("no_dash_tracks", status="blocked")
        return {"status": "pass", "format": "dash", "tracks": tracks,
                "evidence_level": "startup_bytes", "real_device_verified": False}
