import pytest

from sidecar.at.subscription import SubscriptionParseError
from sidecar.t4.adapter import T4Adapter


@pytest.mark.parametrize("headers", [None, "", {}, "{}", '{"Referer":"https://media.example","User-Agent":"test"}'])
def test_header_objects_and_json_strings_are_normalized(headers):
    result = T4Adapter._normalize_play({"url": "https://media.example/video", "header": headers})
    assert isinstance(result["header"], dict)
    if headers and headers != "{}":
        assert result["header"] == {"Referer": "https://media.example", "User-Agent": "test"}


@pytest.mark.parametrize("headers", [
    "not-json-private-secret", "[]", "null", '["private-cookie"]',
    {"Authorization": {"secret": "private-cookie"}}, 42,
])
def test_invalid_headers_are_explicit_and_do_not_expose_values(headers):
    with pytest.raises(SubscriptionParseError) as error:
        T4Adapter._normalize_play({"header": headers})
    assert "private" not in str(error.value)
