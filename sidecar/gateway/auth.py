from __future__ import annotations

import hmac


def gateway_token_matches(expected: str | None, provided: str | None) -> bool:
    """Compare a configured gateway token without exposing either value."""

    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)
