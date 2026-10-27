import base64
import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from sidecar.at.subscription import (
    CachedSubscriptionResolver,
    AtSubscriptionClient,
    SubscriptionParseError,
    SubscriptionParser,
    build_subscription_url,
)
from sidecar.gateway.config import Settings
from sidecar.gateway.main import create_app
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry


AT_BASE = "http://at.example.test:4567"
AT_LOADER = f"{AT_BASE}/Atvp/token/v1.py"
LEGACY_AT_LOADER = f"{AT_BASE}/Atvp.py"


def encoded_ext(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def test_parser_decodes_at_ext_and_classifies_plugin_without_public_secrets() -> None:
    parser = SubscriptionParser(AT_BASE)
    snapshot = parser.parse(
        "demo",
        {
            "sites": [
                {
                    "key": "DemoPlugin",
                    "name": "Demo plugin",
                    "api": "csp_PyProxy",
                    "type": 3,
                    "ext": encoded_ext(
                        {
                            "api": AT_BASE,
                            "source": f"{AT_BASE}/plugins/sub/42.txt",
                            "loader": AT_LOADER,
                            "token": "subscription-token",
                            "secret": "private-secret",
                            "raw": False,
                            "local_proxy_config": {"demo": {"enabled": True}},
                            "filters": [{"name": "detail-normalizer"}],
                        }
                    ),
                }
            ]
        },
    )

    assert snapshot.public_metadata() == {
        "source_id": "demo",
        "site_count": 1,
        "plugins": [
            {
                "id": "DemoPlugin",
                "name": "Demo plugin",
                "type": "secspider",
                "api": "csp_PyProxy",
                "raw": False,
                "filters": 1,
            }
        ],
        "errors": [],
    }
    assert "subscription-token" not in json.dumps(snapshot.public_metadata())
    assert "private-secret" not in json.dumps(snapshot.public_metadata())


def test_parser_classifies_raw_python_and_keeps_builtin_native_plugin() -> None:
    parser = SubscriptionParser(AT_BASE)
    snapshot = parser.parse(
        "demo",
        {
            "sites": [
                {"key": "native", "name": "Native", "api": "csp_AList"},
                {
                    "key": "RawPlugin",
                    "api": "csp_PyProxy",
                    "ext": {
                        "source": f"{AT_BASE}/plugins/sub/7.py",
                        "loader": AT_LOADER,
                        "raw": True,
                    },
                },
            ]
        },
    )

    assert [item.plugin_type for item in snapshot.plugins] == ["builtin", "raw_python"]
    assert snapshot.site_count == 2


def test_builtin_metadata_exposes_route_without_native_credentials() -> None:
    parser = SubscriptionParser(AT_BASE)
    snapshot = parser.parse(
        "demo",
        {
            "sites": [
                {
                    "key": "csp_BiliBili",
                    "name": "BiliBili",
                    "api": "csp_BiliBili",
                    "ext": encoded_ext(
                        {
                            "api": AT_BASE,
                            "token": "private-token",
                            "secret": "private-secret",
                            "uid": "private-uid",
                        }
                    ),
                }
            ]
        },
    )

    metadata = snapshot.public_metadata()["plugins"][0]
    assert metadata["native_route"] == "/bilibili"
    assert metadata["capabilities"] == [
        "home",
        "category",
        "search",
        "detail",
        "play",
    ]
    assert "private-token" not in json.dumps(metadata)
    assert "private-secret" not in json.dumps(metadata)
    assert "private-uid" not in json.dumps(metadata)


def test_parser_marks_page_and_client_local_builtins_without_claiming_t4_support() -> None:
    parser = SubscriptionParser(AT_BASE)
    snapshot = parser.parse(
        "demo",
        {
            "sites": [
                {
                    "key": "home",
                    "name": "WebHome",
                    "api": "csp_WebHome",
                    "homePage": "https://home.example/",
                },
                {
                    "key": "push_agent",
                    "name": "Push",
                    "api": "csp_Push",
                },
                {
                    "key": "nostr",
                    "name": "Nostr",
                    "api": "csp_Nostr",
                    "homePage": "https://nostr.example/",
                },
            ]
        },
    )

    metadata = {item["id"]: item for item in snapshot.public_metadata()["plugins"]}
    assert metadata["home"]["t4_supported"] is False
    assert metadata["home"]["compatibility_mode"] == "page_entry_only"
    assert metadata["home"]["page_entry"] is True
    assert metadata["push_agent"]["compatibility_mode"] == "native_client_passthrough"
    assert metadata["push_agent"]["client_supported"] is False
    assert metadata["push_agent"]["capabilities"] == []
    assert metadata["nostr"]["page_entry"] is True
    assert snapshot.plugin_for("csp_Push").plugin_id == "push_agent"


def test_native_push_keeps_original_jar_ext_and_fixed_client_key() -> None:
    ext = encoded_ext({"api": AT_BASE, "token": "private-token", "secret": "private-secret"})
    site = {
        "key": "renamed-push", "name": "AT Push", "api": "csp_Push",
        "jar": f"{AT_BASE}/spring.jar;md5;fixture", "ext": ext,
        "searchable": 0, "changeable": 0, "type": 3,
    }
    snapshot = SubscriptionParser(AT_BASE).parse("demo", {"sites": [site]})
    plugin = snapshot.plugins[0]
    assert plugin.native_client_site == {**site, "key": "push_agent"}
    metadata = json.dumps(snapshot.public_metadata())
    assert "private" not in metadata
    assert ext not in metadata
    assert plugin.public_metadata()["client_supported"] is True
    assert plugin.public_metadata()["t4_supported"] is False


def test_native_push_can_use_the_subscription_global_spider_jar() -> None:
    snapshot = SubscriptionParser(AT_BASE).parse("demo", {
        "spider": f"{AT_BASE}/spring.jar",
        "sites": [{"key": "push_agent", "api": "csp_Push", "type": 3}],
    })
    assert snapshot.plugins[0].native_client_site["jar"] == f"{AT_BASE}/spring.jar"


def test_piandan_metadata_describes_navigation_not_fake_playback() -> None:
    snapshot = SubscriptionParser(AT_BASE).parse("demo", {
        "sites": [{"key": "csp_PianDan", "api": "csp_PianDan", "indexs": 1}],
    })
    plugin = snapshot.plugins[0]
    assert plugin.site_options["indexs"] == 1
    assert plugin.public_metadata()["capabilities"] == ["home", "category", "search", "detail", "play"]
    assert plugin.public_metadata()["delegated_capabilities"] == ["detail", "play"]
    assert plugin.public_metadata()["compatibility_mode"] == "title_search_navigation"


def test_parser_maps_telegram_builtins_to_their_original_server_routes() -> None:
    parser = SubscriptionParser(AT_BASE)
    snapshot = parser.parse(
        "demo",
        {
            "sites": [
                {"key": "web", "api": "csp_TgWeb"},
                {"key": "channel", "api": "csp_TgChannel"},
            ]
        },
    )

    routes = {
        item["id"]: item["native_route"]
        for item in snapshot.public_metadata()["plugins"]
    }
    assert routes == {"web": "/tg-search", "channel": "/tgsc"}


def test_parser_accepts_legacy_same_origin_atvp_loader() -> None:
    parser = SubscriptionParser(AT_BASE)

    snapshot = parser.parse(
        "demo",
        {
            "sites": [
                {
                    "key": "legacy",
                    "api": "csp_PyProxy",
                    "ext": {
                        "source": f"{AT_BASE}/plugins/sub/7.py",
                        "loader": LEGACY_AT_LOADER,
                        "raw": True,
                    },
                }
            ]
        },
    )

    assert snapshot.errors == ()
    assert snapshot.plugins[0].loader == LEGACY_AT_LOADER


def test_parser_rejects_loader_outside_at_origin() -> None:
    parser = SubscriptionParser(AT_BASE)

    snapshot = parser.parse(
        "demo",
        {
            "sites": [
                {
                    "key": "bad",
                    "ext": {
                        "source": f"{AT_BASE}/plugins/1.txt",
                        "loader": "https://other.example/Atvp/token/v1.py",
                    },
                }
            ]
        },
    )

    assert snapshot.plugins == ()
    assert snapshot.errors == (
        "site[0]: Atvp loader is outside the configured AT origin",
    )


def test_subscription_url_uses_tokenized_at_route() -> None:
    source = SourceConfig(
        source_id="demo",
        display_name="Demo",
        at_base_url=AT_BASE,
        subscription_id="12",
        subscription_token="token-value",
    )

    assert build_subscription_url(source) == f"{AT_BASE}/sub/token-value/12"


def test_subscription_url_accepts_complete_subscription_url() -> None:
    subscription_url = f"{AT_BASE}/sub/token-value/Chajian"
    source = SourceConfig(
        source_id="demo",
        display_name="Demo",
        subscription_url=subscription_url,
    )

    assert build_subscription_url(source) == subscription_url


def test_subscription_client_requests_at_sub_route() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"sites": []})

    source = SourceConfig(
        source_id="demo",
        display_name="Demo",
        at_base_url=AT_BASE,
        subscription_id="12",
        subscription_token="token-value",
    )

    payload = asyncio.run(
        AtSubscriptionClient(httpx.MockTransport(handler)).fetch(source)
    )

    assert payload == {"sites": []}
    assert len(requests) == 1
    assert str(requests[0].url) == f"{AT_BASE}/sub/token-value/12"


def test_subscription_client_redacts_transport_failures() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    source = SourceConfig(
        source_id="demo",
        display_name="Demo",
        at_base_url=AT_BASE,
        subscription_id="12",
        subscription_token="private-token",
    )

    try:
        asyncio.run(
            AtSubscriptionClient(httpx.MockTransport(handler)).fetch(source)
        )
    except SubscriptionParseError as exc:
        assert str(exc) == "AT subscription request failed before receiving a response"
    else:
        raise AssertionError("expected a normalized subscription error")


def test_cached_subscription_resolver_shares_snapshot() -> None:
    calls = 0

    async def fetcher(source: SourceConfig) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"sites": []}

    source = SourceConfig(
        source_id="demo",
        display_name="Demo",
        at_base_url=AT_BASE,
        subscription_id="12",
    )
    resolver = CachedSubscriptionResolver(fetcher, ttl=30)

    async def run() -> None:
        await resolver.resolve(source)
        await resolver.resolve(source)

    asyncio.run(run())

    assert calls == 1


def test_forced_refresh_cannot_be_overwritten_by_an_old_inflight_request() -> None:
    from sidecar.at.subscription import CachedSubscriptionResolver

    source = SourceConfig(
        source_id="demo", display_name="Demo", at_base_url=AT_BASE, subscription_id="12"
    )

    async def run() -> None:
        old_started = asyncio.Event()
        finish_old = asyncio.Event()
        calls = 0

        async def delayed_fetcher(_source):
            nonlocal calls
            calls += 1
            version = calls
            if version == 1:
                old_started.set()
                await finish_old.wait()
            return {"sites": [{"key": f"v{version}", "api": "csp_Media"}]}

        resolver = CachedSubscriptionResolver(delayed_fetcher, ttl=30)
        old = asyncio.create_task(resolver.resolve(source))
        await old_started.wait()
        fresh = await resolver.refresh(source)
        finish_old.set()
        await old
        assert fresh.plugins[0].plugin_id == "v2"
        assert (await resolver.resolve(source)).plugins[0].plugin_id == "v2"
        assert calls == 2

    asyncio.run(run())


def test_subscription_route_force_refresh_bypasses_cache(tmp_path) -> None:
    calls = 0

    async def changing_fetcher(_source):
        nonlocal calls
        calls += 1
        return {"sites": [{"key": f"v{calls}", "api": "csp_Media"}]}

    source = SourceConfig(
        source_id="demo", display_name="Demo", at_base_url=AT_BASE, subscription_id="12"
    )
    app = create_app(
        Settings(plugin_dir=str(tmp_path)), SourceRegistry((source,)), changing_fetcher
    )
    with TestClient(app) as client:
        path = "/t4/api/sources/demo/subscription"
        assert client.get(path).json()["plugins"][0]["id"] == "v1"
        assert client.get(path).json()["plugins"][0]["id"] == "v1"
        assert client.get(path + "?refresh=true").json()["plugins"][0]["id"] == "v2"
        assert client.get(path).json()["plugins"][0]["id"] == "v2"
    assert calls == 2


async def fake_fetcher(source: SourceConfig) -> dict[str, object]:
    assert source.subscription_token == "token-value"
    return {
        "sites": [
            {
                "key": "RawPlugin",
                "name": "Raw plugin",
                "api": "csp_PyProxy",
                "ext": {
                    "source": f"{AT_BASE}/plugins/sub/7.py",
                    "loader": AT_LOADER,
                    "raw": True,
                },
            }
        ]
    }


def test_subscription_route_returns_redacted_snapshot() -> None:
    source = SourceConfig(
        source_id="demo",
        display_name="Demo",
        at_base_url=AT_BASE,
        subscription_id="12",
        subscription_token="token-value",
    )
    app = create_app(
        Settings(),
        SourceRegistry((source,)),
        fake_fetcher,
    )

    response = TestClient(app).get("/t4/api/sources/demo/subscription")

    assert response.status_code == 200
    assert response.json()["plugins"][0]["type"] == "raw_python"
    assert response.json()["plugins"][0]["fm_path"].startswith(
        "/t4/fm/demo/RawPlugin/"
    )
    assert "token-value" not in response.text


def test_app_registers_environment_subscription_when_source_file_is_missing() -> None:
    settings = Settings(
        source_config_file="missing-sources.json",
        subscription_url=f"{AT_BASE}/sub/token-value/Chajian",
    )
    app = create_app(settings, t4_adapter=object())

    response = TestClient(app).get("/t4/api/sources")

    assert response.status_code == 200
    assert response.json() == {
        "sources": [
            {
                "id": "env-at",
                "name": "AT environment subscription",
                "status": "configured",
                "capabilities": ["subscription", "t4"],
            }
        ]
    }
