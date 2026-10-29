from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


@dataclass(frozen=True, slots=True)
class ImageHeaderRule:
    host: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    pattern: re.Pattern[str] | None = field(repr=False)

    def matches(self, host: str) -> bool:
        # FongMi Util.containOrMatch: literal containment OR regex full match.
        return self.host in host or bool(self.pattern and self.pattern.fullmatch(host))


def parse_image_header_rules(value: Any) -> tuple[ImageHeaderRule, ...]:
    if not isinstance(value, list):
        return ()
    rules = []
    for item in value:
        if not isinstance(item, dict):
            continue
        host, headers = item.get("host", ""), item.get("header")
        if host is None:
            host = ""
        if not isinstance(host, str) or not isinstance(headers, dict):
            continue
        cleaned = {
            key.lower(): val.strip(" \t")
            for key, val in headers.items()
            if isinstance(key, str) and isinstance(val, str)
            and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
            and "\r" not in val and "\n" not in val
        }
        if not cleaned:
            continue
        try:
            pattern = re.compile(host)
        except re.error:
            pattern = None
        rules.append(ImageHeaderRule(host, cleaned, pattern))
    return tuple(rules)


def match_image_headers(host: str, rules: tuple[ImageHeaderRule, ...]) -> dict[str, str]:
    headers = {}
    for rule in rules:
        if rule.matches(host):
            headers.update(rule.headers)
    return headers
