from sidecar.gateway.auth import gateway_token_matches


def test_gateway_token_is_optional_for_local_mode() -> None:
    assert gateway_token_matches(None, None) is True
    assert gateway_token_matches(None, "token") is True
    assert gateway_token_matches("token", None) is False
    assert gateway_token_matches("", "token") is True


def test_gateway_token_matches_exact_value() -> None:
    assert gateway_token_matches("secret-token", "secret-token") is True
    assert gateway_token_matches("secret-token", "other-token") is False
