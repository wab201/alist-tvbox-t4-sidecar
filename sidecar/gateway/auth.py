from __future__ import annotations

import hmac


def gateway_token_matches(expected: str | None, provided: str | None) -> bool:
    """Allow local mode when no token is configured; otherwise compare exactly."""

    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)
