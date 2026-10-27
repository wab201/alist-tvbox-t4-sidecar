import base64
import json

import pytest
from fastapi.testclient import TestClient

from sidecar.gateway.config import Settings
from sidecar.gateway.fm import build_fm_path
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.t4.adapter import T4Request


class FakeAdapter:
    def __init__(self) -> None:
        self.requests: list[T4Request] = []

    async def dispatch(self, request: T4Request) -> dict[str, object]:
        self.requests.append(request)
        return {"class": "t4", "list": []}


def test_fm_path_is_direct() -> None:
    assert build_fm_path("env-at", "csp_BiliBili") == (
        "/t4/fm/env-at/csp_BiliBili/"
    )


def test_fm_path_quotes_source_and_plugin_without_extra_credentials() -> None:
    assert build_fm_path("AT source", "Bili?name=#1") == (
        "/t4/fm/AT%20source/Bili%3Fname%3D%231/"
    )


def test_fm_route_injects_source_and_plugin_without_client_parameters() -> None:
    adapter = FakeAdapter()
    source = SourceConfig("env-at", "AT subscription", status="configured")
    app = create_app(
        Settings(),
        SourceRegistry((source,)),
        t4_adapter=adapter,
    )
    response = TestClient(app).get(
        "/t4/fm/env-at/csp_BiliBili/api/home"
    )

    assert response.status_code == 200
    assert adapter.requests == [
        T4Request(
            module="home",
            source_id="env-at",
            plugin_id="csp_BiliBili",
            params={},
            proxy_base="http://testserver/",
        )
    ]


def test_fm_signed_route_is_removed() -> None:
    source = SourceConfig("env-at", "AT subscription", status="configured")
    app = create_app(
        Settings(),
        SourceRegistry((source,)),
        t4_adapter=FakeAdapter(),
    )

    response = TestClient(app).get(
        "/t4/fm/env-at/csp_BiliBili/invalid-key/api/home"
    )

    assert response.status_code == 404


def test_fm_route_is_direct() -> None:
    source = SourceConfig("env-at", "AT subscription", status="configured")
    app = create_app(
        Settings(),
        SourceRegistry((source,)),
        t4_adapter=FakeAdapter(),
    )

    response = TestClient(app).get(
        "/t4/fm/env-at/csp_BiliBili/api/home"
    )

    assert response.status_code == 200


@pytest.mark.parametrize("params,module", [
    ({"filter": "true"}, "home"),
    ({"ac": "detail", "t": "0", "pg": "2"}, "category"),
    ({"ac": "list", "t": "films"}, "category"),
    ({"wd": "test", "quick": "false"}, "search"),
    ({"ac": "detail", "ids": "opaque"}, "detail"),
    ({"play": "opaque", "flag": "main"}, "play"),
    ({"ac": "action", "action": "refresh"}, "action"),
])
@pytest.mark.parametrize("transport", ["GET", "JSON", "FORM"])
def test_fm_base_address_dispatches_standard_client_parameters(params, module, transport):
    adapter = FakeAdapter()
    app = create_app(
        Settings(), SourceRegistry((SourceConfig("env-at", "AT"),)), t4_adapter=adapter
    )
    with TestClient(app) as client:
        path = "/t4/fm/env-at/bili/"
        if transport == "GET":
            response = client.get(path, params=params)
        elif transport == "JSON":
            response = client.post(path, json=params)
        else:
            response = client.post(path, data=params)
    assert response.status_code == 200
    assert adapter.requests[0].module == module
    assert adapter.requests[0].source_id == "env-at"
    assert adapter.requests[0].plugin_id == "bili"


@pytest.mark.parametrize("encoding", ["urlsafe", "standard", "json"])
def test_fm_category_decodes_client_filter_without_using_site_extend(encoding):
    adapter = FakeAdapter()
    app = create_app(
        Settings(), SourceRegistry((SourceConfig("env-at", "AT"),)), t4_adapter=adapter
    )
    filters = {"genre": "科幻", "sort": "time"}
    text = json.dumps(filters, ensure_ascii=False)
    ext = text
    if encoding == "urlsafe":
        ext = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    elif encoding == "standard":
        ext = base64.b64encode(text.encode()).decode()
    response = TestClient(app).get(
        "/t4/fm/env-at/bili/",
        params={"ac": "detail", "t": "all", "ext": ext, "extend": "site-config"},
        headers={"X-CLIENT": "com.fongmi.android.tv"},
    )
    assert response.status_code == 200
    assert adapter.requests[0].params["extend"] == filters
    assert adapter.requests[0].params["client"] == "com.fongmi.android.tv"


@pytest.mark.parametrize("ext", ["%%%", "[]", "123", "not base64"])
def test_invalid_fm_filter_fails_before_dispatch(ext):
    adapter = FakeAdapter()
    app = create_app(
        Settings(), SourceRegistry((SourceConfig("env-at", "AT"),)), t4_adapter=adapter
    )
    response = TestClient(app).get(
        "/t4/fm/env-at/bili/", params={"t": "all", "ext": ext}
    )
    assert response.status_code == 400
    assert adapter.requests == []


def test_malformed_fm_post_is_not_mistaken_for_home():
    adapter = FakeAdapter()
    app = create_app(
        Settings(), SourceRegistry((SourceConfig("env-at", "AT"),)), t4_adapter=adapter
    )
    client = TestClient(app)
    assert client.post(
        "/t4/fm/env-at/bili/", content="{", headers={"Content-Type": "application/json"}
    ).status_code == 400
    assert client.post("/t4/fm/env-at/bili/", json=[]).status_code == 400
    assert adapter.requests == []
