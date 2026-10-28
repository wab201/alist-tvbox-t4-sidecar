from __future__ import annotations

import re
import zlib
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx
import m3u8


class HlsError(ValueError):
    pass


def png_ts_offset(data: bytes) -> int:
    """Recognize the observed PNG IDAT wrapper, never scan arbitrary payloads."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return 0
    position = 8
    while position + 12 <= min(len(data), 8192):
        size = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        start = position + 8
        if kind == b"IDAT":
            for offset in range(start, min(start + 256, len(data) - 188 * 8 + 1)):
                packets = [data[offset + n * 188:offset + (n + 1) * 188] for n in range(8)]
                if all(
                    packet[0] == 0x47 and not packet[1] & 0x80
                    and (packet[3] >> 4) & 3
                    and ((packet[3] >> 4) & 3 == 1 or packet[4] <= 183)
                    for packet in packets
                ) and any(((packet[1] & 31) << 8) | packet[2] == 0 for packet in packets):
                    return offset
            raise HlsError("invalid_png_transport_stream")
        end = start + size
        if size > 8192 or end + 4 > len(data):
            raise HlsError("invalid_png_transport_stream")
        if position == 8 and (kind != b"IHDR" or size != 13):
            raise HlsError("invalid_png_transport_stream")
        if kind not in {b"IHDR", b"tEXt", b"iTXt", b"zTXt", b"PLTE", b"tRNS", b"gAMA", b"sRGB", b"pHYs"}:
            raise HlsError("invalid_png_transport_stream")
        if zlib.crc32(data[position + 4:end]) & 0xFFFFFFFF != int.from_bytes(data[end:end + 4], "big"):
            raise HlsError("invalid_png_transport_stream")
        position = end + 4
    raise HlsError("invalid_png_transport_stream")


def rewrite_manifest(data: bytes, url: str, register: Callable[[str, str], str]) -> bytes:
    if len(data) > 2_097_152 or not data.lstrip().startswith(b"#EXTM3U"):
        raise HlsError("invalid_hls_manifest")
    try:
        playlist = m3u8.loads(data.decode("utf-8-sig"), uri=url)
    except (ValueError, UnicodeError) as exc:
        raise HlsError("invalid_hls_manifest") from exc
    if not playlist.playlists and not playlist.segments:
        raise HlsError("empty_hls_manifest")
    rewritten = set()
    public_uris = set()

    def replace(item, role: str) -> None:
        if item is not None and item.uri and id(item) not in rewritten:
            absolute = urljoin(url, item.uri)
            if urlsplit(absolute).scheme not in {"http", "https"}:
                raise HlsError("unsupported_hls_uri")
            item.uri = register(absolute, role)
            rewritten.add(id(item))
            public_uris.add(item.uri)

    for item in [*playlist.playlists, *playlist.iframe_playlists, *playlist.media,
                 *playlist.rendition_reports]:
        replace(item, "hls")
    for item in [*playlist.keys, *playlist.session_keys, *playlist.session_data]:
        replace(item, "binary")
    for segment in playlist.segments:
        encrypted = segment.key is not None and segment.key.method not in {None, "NONE"}
        replace(segment, "binary" if encrypted or segment.byterange else "segment")
        replace(segment.init_section, "binary")
        for part in segment.parts:
            replace(part, "binary")
    replace(playlist.preload_hint, "binary")
    result = playlist.dumps().encode("utf-8")
    # Do not silently pass through unsupported URI-bearing extensions.
    for match in re.finditer(rb'URI="([^"]+)"', result):
        if match[1].decode("utf-8") not in public_uris:
            raise HlsError("unsupported_hls_uri")
    return result


def content_range(headers: httpx.Headers) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("content-range", ""))
    if match is None:
        return None
    start, end, total = map(int, match.groups())
    if not 0 <= start <= end < total:
        raise HlsError("invalid_media_range")
    return start, end, total


class ClosingStream(httpx.SyncByteStream):
    def __init__(self, chunks, close: Callable[[], None]) -> None:
        self.chunks = chunks
        self._close = close

    def __iter__(self):
        try:
            yield from self.chunks
        finally:
            self.close()

    def close(self) -> None:
        self._close()
