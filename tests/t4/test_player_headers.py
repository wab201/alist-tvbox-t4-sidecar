import json

from fastapi import FastAPI, Request
import httpx
import pytest

from sidecar.at.subscription import SubscriptionParseError
from sidecar.t4.adapter import T4Adapter
from tests.unit.test_media_disconnect import serving


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


@pytest.mark.parametrize("as_json", [False, True])
def test_header_outer_whitespace_is_trimmed_without_changing_internal_values(as_json):
    headers = {
        "Referer": " \thttps://media.example/path?sign=exact%2Fvalue\t ",
        "User-Agent": "\tplayer  agent/1.0 ",
        "Cookie": " session=fixture;  preference=one two \t",
    }
    original = dict(headers)
    result = T4Adapter._normalize_play({"header": json.dumps(headers) if as_json else headers})
    assert result["header"] == {
        "Referer": "https://media.example/path?sign=exact%2Fvalue",
        "User-Agent": "player  agent/1.0",
        "Cookie": "session=fixture;  preference=one two",
    }
    assert headers == original


def test_header_cleanup_does_not_hide_line_breaks():
    result = T4Adapter._normalize_play({"header": {"Referer": "\r\nhttps://media.example/\r\n"}})
    assert result["header"]["Referer"] == "\r\nhttps://media.example/\r\n"


def test_normalized_headers_reach_a_real_http_server():
    app = FastAPI()
    received = []

    @app.get("/video")
    async def video(request: Request):
        received.append(dict(request.headers))
        return {"ok": True}

    with serving(app) as origin, httpx.Client(trust_env=False, timeout=4) as client:
        result = T4Adapter._normalize_play({
            "url": origin + "/video?sign=exact%2Fvalue",
            "header": {"Referer": origin + "/ \t", "User-Agent": "\tplayer  agent "},
        })
        response = client.get(result["url"], headers=result["header"])
    assert response.status_code == 200
    assert received[0]["referer"] == origin + "/"
    assert received[0]["user-agent"] == "player  agent"
