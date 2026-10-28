from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import unquote, urljoin, urlsplit

from lxml import html


class ServerParseError(ValueError):
    pass


def extract_media(data: bytes, page_url: str) -> str | None:
    """Only declarative player data; no arbitrary JavaScript execution or sniffing."""
    try:
        document = html.fromstring(data, base_url=page_url)
    except (ValueError, html.etree.ParserError):
        return None
    candidates = document.xpath("//video/@src | //video/source/@src | //audio/@src | //audio/source/@src")
    text = data.decode("utf-8", errors="replace")
    for match in re.finditer(r"\b(?:var\s+)?player_[A-Za-z0-9_]+\s*=\s*", text):
        try:
            player, _ = json.JSONDecoder().raw_decode(text[match.end():])
            if not isinstance(player, dict) or not isinstance(player.get("url"), str):
                continue
            value = player["url"]
            encryption = str(player.get("encrypt", 0))
            if encryption == "1":
                value = unquote(value)
            elif encryption == "2":
                value = unquote(base64.b64decode(value, validate=True).decode("utf-8"))
            elif encryption != "0":
                continue
            candidates.append(value)
        except (ValueError, UnicodeError):
            continue
    for candidate in candidates:
        absolute = urljoin(page_url, candidate.strip())
        parsed = urlsplit(absolute)
        if parsed.scheme in {"http", "https"} and parsed.path.lower().endswith((".m3u8", ".mp4", ".mpd", ".webm")):
            return absolute
    return None


def resolve_page(url: str, headers: dict, request) -> tuple[str, dict, str]:
    origin = urlsplit(url)
    started = time.monotonic()
    with request(url, headers) as response:
        if response.status_code != 200:
            raise ServerParseError("server_parser_upstream_status")
        mime = response.headers.get("content-type", "").split(";")[0].lower()
        if mime in {"video/mp4", "video/webm"}:
            return str(response.url), headers, mime
        chunks, size = [], 0
        for chunk in response.iter_bytes(16384):
            size += len(chunk)
            if size > 1_048_576 or time.monotonic() - started > 20:
                raise ServerParseError("server_parser_limit")
            chunks.append(chunk)
        content = b"".join(chunks)
        final = str(response.url)
        if content.lstrip().startswith(b"#EXTM3U"):
            return final, headers, "application/vnd.apple.mpegurl"
        target = extract_media(content, final)
        if target is None:
            raise ServerParseError("server_parser_unresolved")
        result_headers = {
            key: value for key, value in headers.items()
            if key.lower() not in {"cookie", "authorization", "host"}
            or (urlsplit(target).scheme, urlsplit(target).netloc) == (origin.scheme, origin.netloc)
        }
        result_headers = {k: v for k, v in result_headers.items() if k.lower() != "referer"}
        result_headers["Referer"] = final
        if urlsplit(target).path.lower().endswith(".mpd"):
            raise ServerParseError("server_parser_dash_unsupported")
        return target, result_headers, ""
