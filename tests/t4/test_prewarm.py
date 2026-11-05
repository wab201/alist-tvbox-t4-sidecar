import asyncio
from dataclasses import replace
import shutil
import threading

import httpx
import pytest

from sidecar.at.native import NativeAtClient, NativeAtInvoker, NativeAtUnsupportedError, NativeAtError
from sidecar.at.subscription import SubscriptionSnapshot
from sidecar.gateway.profiles import T4Profile, PluginPolicyDenied
from sidecar.t4.adapter import AtvpWorkerInvoker, T4Adapter, T4Request
from sidecar.t4.prewarm import NextEpisodeWarmup
from tests.t4.test_adapter import descriptor, source, FakeWorker
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.gateway.uploads import UploadedPluginStore
from sidecar.worker.client import WorkerConfig, WorkerPool
from tests.worker.test_secspider_runtime import build_secspider_fixture


def cloud(**kwargs):
    return replace(descriptor(), plugin_type="builtin", api="csp_AList",
                   backend_api="https://at.example", **kwargs)


def detail():
    return {"list": [{
        "vod_id": "folder", "vod_play_from": "line-a$$$line-b",
        "vod_play_url": "1$1@11#2$1@12#3$1@13$$$1$1@21#2$1@22",
    }]}


@pytest.mark.parametrize("kind", [
    "builtin", "raw_python", "secspider", "javascript", "official_t4",
])
def test_playlist_scope_accepts_opaque_urls_logical_ids_for_every_runtime(kind):
    target = replace(descriptor(), plugin_type=kind)
    for play_id in ("1@12", "https://page.example/watch/2", "msubep-1-2",
                    "uuid-file-abc", "eyJmaWxlIjoiMiJ9", "1@12-3"):
        assert NextEpisodeWarmup.eligible(target, play_id)
    for play_id in ("", "magnet:?xt=x", "push://abc", "action://delete",
                    "msubdel-1", "msubinspect-1", "atvp_detail:folder",
                    "1@12\n", "x" * 8193):
        assert not NextEpisodeWarmup.eligible(target, play_id)


def test_native_prefetch_timeout_and_policy_never_download_media():
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.url.path == "/play/private-token"
        assert request.url.params["id"] == "1@12"
        assert request.url.params["type"] == "t4-direct"
        assert request.extensions["timeout"] == {
            "connect": 3.0, "read": 3.0, "write": 3.0, "pool": 3.0,
        }
        return httpx.Response(200, json={"url": "https://cdn.example/next.mp4"})

    client = NativeAtClient(transport=httpx.MockTransport(upstream))
    try:
        client.prefetch(cloud(), {"id": "1@12"})
        with pytest.raises(NativeAtUnsupportedError):
            client.prefetch(cloud(), {"id": "msubadd-1"})
        assert len(calls) == 1
    finally:
        client.close()


def test_prefetch_response_is_bounded_even_when_upstream_returns_large_json():
    class LargeBody(httpx.SyncByteStream):
        closed = False
        chunks = 0

        def __iter__(self):
            for _ in range(100):
                self.chunks += 1
                yield b"x" * 8192

        def close(self):
            self.closed = True

    body = LargeBody()
    client = NativeAtClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=body),
    ))
    try:
        with pytest.raises(NativeAtError) as error:
            client.prefetch(cloud(), {"id": "1@12"})
        assert error.value.reason == "prefetch_budget"
        assert body.closed and body.chunks == 9
    finally:
        client.close()


def test_warmup_result_and_attempt_caches_stay_within_sixteen_entries():
    async def scenario():
        warm = NextEpisodeWarmup(delay=0)
        target, origin = cloud(), source()
        rows = detail()
        rows["list"][0]["vod_play_from"] = "line-a"
        rows["list"][0]["vod_play_url"] = "#".join(f"{i}$1@{i}" for i in range(30))
        warm.remember(origin, target, rows)

        async def resolve(params):
            return {"url": f"https://cdn.example/{params['id']}.mp4"}

        for i in range(25):
            warm.schedule(origin, target, {"id": f"1@{i}", "flag": "line-a"},
                          "", None, resolve)
            await warm.task
        assert len(warm.results) == len(warm.attempts) == 16
        await warm.aclose()

    asyncio.run(scenario())


def test_same_line_next_only_one_background_task_and_single_use_cache():
    async def scenario():
        warm = NextEpisodeWarmup(delay=0)
        target, origin = cloud(), source()
        warm.remember(origin, target, detail())
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def resolve(params):
            calls.append(params["id"])
            entered.set()
            await release.wait()
            return {"url": "https://cdn.example/next.mp4", "header": {"X-Test": "one"}}

        params = {"id": "1@11", "flag": "line-a"}
        warm.schedule(origin, target, params, "https://gateway.example", None, resolve)
        await entered.wait()
        warm.schedule(origin, target, params, "https://gateway.example", None, resolve)
        warm.schedule(origin, target, {**params, "id": "1@21", "flag": "line-b"},
                      "https://gateway.example", None, resolve)
        assert calls == ["1@12"]
        release.set()
        await warm.task
        key = warm.key(origin, target, {**params, "id": "1@12"},
                       "https://gateway.example", None)
        assert warm.take(key)["url"] == "https://cdn.example/next.mp4"
        assert warm.take(key) is None
        warm.schedule(origin, target, params, "https://gateway.example", None, resolve)
        assert calls == ["1@12"]
        await warm.aclose()
        assert not warm.links and not warm.results

    asyncio.run(scenario())


def test_short_ttl_capacity_expiry_and_execution_context_isolation():
    async def scenario():
        now = [100.0]
        warm = NextEpisodeWarmup(clock=lambda: now[0], delay=0)
        target, origin = cloud(), source()
        rows = detail()
        rows["list"][0]["vod_play_from"] = "line-a"
        rows["list"][0]["vod_play_url"] = "#".join(f"{i}$1@{i}" for i in range(600))
        warm.remember(origin, target, rows)
        assert len(warm.links) == 512
        params = {"id": "1@599", "flag": "line-a"}
        profile = T4Profile()
        key = warm.key(origin, target, params, "https://gateway.example", profile)
        for changed in (
            (replace(origin, cloud_playback="server"), target, params, "https://gateway.example", profile),
            (origin, replace(target, token="changed"), params, "https://gateway.example", profile),
            (origin, target, {**params, "client": "other"}, "https://gateway.example", profile),
            (origin, target, params, "https://other.example", profile),
            (origin, target, params, "https://gateway.example", replace(profile, mode="blacklist")),
        ):
            assert warm.key(*changed) != key
        warm.results[key] = (now[0], {"url": "https://cdn.example/next.mp4"})
        now[0] += 45
        assert warm.take(key) is None
        now[0] += 21600 - 45
        assert warm.prune() == 512
        await warm.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("result", [
    {"msg": "not media"}, {"url": "push://resource"},
    {"url": "https://page.example", "parse": 1},
    {"url": "https://cdn.example/next.mp4", "msg": "x" * 65536},
])
def test_bad_results_are_not_cached_and_prewarm_does_not_chain(result):
    async def scenario():
        warm = NextEpisodeWarmup(delay=0)
        warm.remember(source(), cloud(), detail())
        calls = []

        async def resolve(params):
            calls.append(params["id"])
            return result

        warm.schedule(source(), cloud(), {"id": "1@11", "flag": "line-a"}, "", None, resolve)
        await warm.task
        assert not warm.results
        assert calls == ["1@12"]
        await warm.aclose()

    asyncio.run(scenario())


def test_adapter_warmed_next_returns_normal_play_result_without_calling_media_or_worker():
    async def scenario():
        requests = []

        def upstream(request):
            requests.append(request)
            if request.url.path.startswith("/vod"):
                return httpx.Response(200, json=detail())
            assert request.url.path.startswith("/play")
            play_id = request.url.params["id"]
            return httpx.Response(200, json={"url": f"https://cdn.example/{play_id}.mp4"})

        client = NativeAtClient(transport=httpx.MockTransport(upstream))
        worker = FakeWorker()
        target, origin = cloud(), source()
        adapter = T4Adapter(
            SourceRegistry((origin,)), worker, native_invoker=NativeAtInvoker(client),
            local_snapshot_provider=lambda _: SubscriptionSnapshot(origin.source_id, 1, (target,)),
        )
        adapter._prewarm.delay = 0
        try:
            request = lambda module, params, profile=None: T4Request(
                module, origin.source_id, target.plugin_id, params, "https://gateway.example", profile,
            )
            payload = await adapter.dispatch(request("detail", {"ids": "folder"}))
            ids = [entry.split("$", 1)[1] for entry in
                   payload["list"][0]["vod_play_url"].split("$$$")[0].split("#")]
            await adapter.dispatch(request("play", {"play": ids[0], "flag": "line-a"}))
            await adapter._prewarm.task
            assert len(requests) == 3  # detail, current resolver, next resolver only
            result = await adapter.dispatch(request("play", {"play": ids[1], "flag": "line-a"}))
            assert result["url"] == "https://cdn.example/1@12.mp4"
            assert len(requests) == 3
            assert not worker.calls
            blocked = T4Profile(mode="whitelist")
            with pytest.raises(PluginPolicyDenied):
                await adapter.dispatch(request("play", {"play": ids[2], "flag": "line-a"}, blocked))
        finally:
            await adapter.aclose()
            adapter.close()
            client.close()

    asyncio.run(scenario())


def test_failed_prewarm_is_not_repeated_and_does_not_affect_foreground():
    async def scenario():
        warm = NextEpisodeWarmup(delay=0)
        warm.remember(source(), cloud(), detail())
        calls = []

        async def resolve(params):
            calls.append(params["id"])
            raise TimeoutError("fixture")

        params = {"id": "1@11", "flag": "line-a"}
        warm.schedule(source(), cloud(), params, "", None, resolve)
        await warm.task
        warm.schedule(source(), cloud(), params, "", None, resolve)
        assert calls == ["1@12"] and not warm.results
        await warm.aclose()

    asyncio.run(scenario())


def test_refresh_discards_inflight_result_without_abandoning_resolver():
    async def scenario():
        warm = NextEpisodeWarmup(delay=0)
        warm.remember(source(), cloud(), detail())
        entered, release = asyncio.Event(), asyncio.Event()

        async def resolve(params):
            entered.set()
            await release.wait()
            return {"url": "https://cdn.example/next.mp4"}

        warm.schedule(source(), cloud(), {"id": "1@11", "flag": "line-a"}, "", None, resolve)
        await entered.wait()
        warm.invalidate(source().source_id, cloud().plugin_id)
        release.set()
        await warm.task
        assert not warm.results and not warm.links
        await warm.aclose()

    asyncio.run(scenario())


def test_foreground_does_not_wait_for_pending_warmup_and_gate_can_disable_it():
    async def scenario():
        target, origin = cloud(), source()
        entered, release = asyncio.Event(), asyncio.Event()

        class Native:
            calls = []

            def call(self, key, plugin, method, params):
                self.calls.append((method, params))
                if method == "detail":
                    return detail()
                return {"url": "https://cdn.example/current.mp4"}

            def prefetch(self, *args):
                raise AssertionError("unused: replace resolve for deterministic concurrency")

        native = Native()
        make = lambda **kwargs: T4Adapter(
            SourceRegistry((origin,)), FakeWorker(), native_invoker=native,
            local_snapshot_provider=lambda _: SubscriptionSnapshot(origin.source_id, 1, (target,)),
            **kwargs,
        )
        adapter = make()
        adapter._prewarm.delay = 0

        async def resolve(params):
            entered.set()
            await release.wait()
            return {"url": "https://cdn.example/warm.mp4"}

        try:
            adapter._prewarm.remember(origin, target, detail())
            adapter._prewarm.schedule(
                origin, target, {"id": "1@11", "flag": "line-a"}, "", None, resolve,
            )
            await entered.wait()
            result = await asyncio.wait_for(adapter.dispatch(T4Request(
                "play", origin.source_id, target.plugin_id, {"play": "1@12", "flag": "line-a"},
            )), timeout=1)
            assert result["url"] == "https://cdn.example/current.mp4"
            assert not adapter._prewarm.task.done()
            gate = make(prewarm=False)
            assert gate._prewarm is None
            gate.close()
        finally:
            release.set()
            await adapter.aclose()
            adapter.close()

    asyncio.run(scenario())


def test_actions_invalid_entries_folders_last_episode_and_lines_never_cross():
    warm = NextEpisodeWarmup()
    target, origin = descriptor(), source()
    warm.remember(origin, target, {"list": [{
        "vod_play_from": "HD$$$SD$$$操作",
        "vod_play_url": (
            "1$ep-a#2$ep-b#收藏此剧$keep-me#3$ep-c#4$action://delete#5$ep-d"
            "$$$1$ep-x#2$ep-y$$$a$delete-one#b$delete-two"
        ),
    }, {
        "vod_tag": "folder", "vod_play_from": "HD", "vod_play_url": "1$f1#2$f2",
    }]})
    pairs = {(key[2], key[3]): item[2] for key, item in warm.links.items()}
    assert pairs == {("HD", "ep-a"): "ep-b", ("SD", "ep-x"): "ep-y"}
    warm.remember(origin, target, {"list": [{
        "vod_play_from": "HD", "vod_play_url": "only$ep-a",
    }]})
    assert ("HD", "ep-a") not in {(key[2], key[3]) for key in warm.links}
    warm.close()


@pytest.mark.parametrize("api,path,parameter,play_id", [
    ("csp_AList", "/play/private-token", "id", "1@12"),
    ("csp_Media", "/play/private-token", "id", "msubep-20-2"),
    ("csp_BiliBili", "/play/private-token", "bvid", "BV123:2"),
    ("csp_Emby", "/emby-play/private-token", "id", "uuid-next"),
    ("csp_Jellyfin", "/jellyfin-play/private-token", "id", "uuid-next"),
    ("csp_FeiNiu", "/feiniu-play/private-token", "id", "uuid-next"),
    ("csp_Live", "/live-play/private-token", "id", "channel-2"),
])
def test_native_prefetch_uses_the_normal_plugin_route_and_query(api, path, parameter, play_id):
    requests = []
    client = NativeAtClient(transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(
            200, json={"url": "https://cdn.example/next.mp4"},
        ),
    ))
    try:
        target = replace(cloud(), api=api)
        params = {"id": play_id, "flag": "line", "_server_proxy": True}
        normal = client.call(target, "play", params)
        assert client.prefetch(target, params) == normal
        first, second = requests
        assert second.url.path == path
        assert second.url.params[parameter] == play_id
        assert second.url == first.url
        assert second.headers["Accept-Encoding"] == "identity"
        assert second.extensions["timeout"]["read"] == 3.0
    finally:
        client.close()


@pytest.mark.parametrize("kind,api", [
    ("official_t4", "csp_PyProxy"), ("builtin", "csp_Media"),
])
def test_official_t4_prefetch_keeps_endpoint_flag_and_size_budget(kind, api):
    calls = []
    target = replace(cloud(), plugin_type=kind, api=api,
                     t4_endpoint="https://official.example/t4/v/fixture")
    client = NativeAtClient(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(
            200, json={"url": "https://cdn.example/next.mp4"},
        ),
    ))
    try:
        client.prefetch(target, {"id": "logical-episode", "flag": "HD"})
        assert str(calls[0].url).startswith(target.t4_endpoint)
        assert dict(calls[0].url.params) == {"play": "logical-episode", "flag": "HD"}
        assert calls[0].extensions["timeout"]["read"] == 3.0
    finally:
        client.close()


@pytest.mark.parametrize("api,play_id", [
    ("csp_Live", "https://cdn.example/live.m3u8"),
    ("csp_TgWeb", "https://tg.example/v/episode2"),
])
def test_inline_media_prefetch_does_not_probe_or_download_the_media(api, play_id):
    def reject(request):
        raise AssertionError("inline playback needs no HTTP request")
    client = NativeAtClient(transport=httpx.MockTransport(reject))
    try:
        assert client.prefetch(replace(cloud(), api=api), {"id": play_id})["url"] == play_id
    finally:
        client.close()


@pytest.mark.parametrize("kind", ["raw_python", "secspider", "javascript"])
def test_common_adapter_covers_worker_types_and_keeps_proxy_and_request_context(kind):
    class Worker(FakeWorker):
        def call(self, key, plugin, method, params):
            self.calls.append((key, method, dict(params)))
            if method == "detail":
                return {"list": [{
                    "vod_id": "show", "vod_play_from": "HD$$$SD",
                    "vod_play_url": "1$opaque-a#2$opaque-b$$$1$other-a#2$other-b",
                }]}
            return {"url": "https://cdn.example/current.mp4"}

        def prefetch(self, key, plugin, params):
            self.calls.append((key, "prefetch", dict(params)))
            return {"url": "https://gateway.example/t4/proxy/fixture?episode=2",
                    "header": '{"User-Agent":"fixture"}'}

    async def scenario():
        target = replace(descriptor(), plugin_type=kind)
        origin = replace(source(), cloud_playback="server")
        worker = Worker()
        adapter = T4Adapter(SourceRegistry((origin,)), worker,
                            local_snapshot_provider=lambda _: SubscriptionSnapshot(
                                origin.source_id, 1, (target,)))
        adapter._prewarm.delay = 0
        req = lambda module, params: T4Request(
            module, origin.source_id, target.plugin_id,
            {**params, "client": "fixture-client"}, "https://gateway.example",
        )
        try:
            payload = await adapter.dispatch(req("detail", {"ids": "show"}))
            ids = [part.partition("$")[2] for part in
                   payload["list"][0]["vod_play_url"].split("$$$")[0].split("#")]
            await adapter.dispatch(req("play", {"play": ids[0], "flag": "HD"}))
            await adapter._prewarm.task
            assert [method for _, method, _ in worker.calls] == ["detail", "play", "prefetch"]
            params = worker.calls[-1][2]
            assert params["id"] == "opaque-b" and params["flag"] == "HD"
            assert params["_server_proxy"] is True
            assert params["_proxy_base"] == "https://gateway.example"
            assert params["client"] == "fixture-client"
            result = await adapter.dispatch(req("play", {"play": ids[1], "flag": "HD"}))
            assert result["url"].startswith("https://gateway.example/t4/proxy/")
            assert result["header"] == {"User-Agent": "fixture"}
            assert len(worker.calls) == 3
        finally:
            await adapter.aclose()
            adapter.close()

    asyncio.run(scenario())


def test_worker_prefetch_never_initializes_or_queues_behind_foreground():
    entered, release = threading.Event(), threading.Event()

    class Pool:
        requests = []
        warms = []

        def request(self, key, method, params):
            self.requests.append(method)
            if method == "play":
                entered.set()
                assert release.wait(3)
            return {}

        def prefetch(self, key, params):
            self.warms.append(params)
            return {"url": "https://cdn.example/next.mp4"}

    async def scenario():
        pool = Pool()
        invoker = AtvpWorkerInvoker(pool)
        target = descriptor()
        assert invoker.prefetch("key", target, {"id": "next"}) is None
        assert not pool.requests and not pool.warms
        await asyncio.to_thread(invoker.call, "key", target, "home", {})
        foreground = asyncio.create_task(asyncio.to_thread(
            invoker.call, "key", target, "play", {"id": "current"},
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert await asyncio.to_thread(invoker.prefetch, "key", target, {"id": "next"}) is None
            assert not pool.warms
        finally:
            release.set()
            await foreground
        assert invoker.prefetch("key", target, {"id": "next"})["url"]
        assert pool.requests == ["init", "home", "play"]

    asyncio.run(scenario())


def test_clicking_same_next_while_worker_is_warming_reuses_the_inflight_result():
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        class Worker(FakeWorker):
            warms = 0

            def prefetch(self, key, plugin, params):
                self.warms += 1
                entered.set()
                assert release.wait(3)
                return {"url": "https://cdn.example/warmed.mp4"}

        worker = Worker()
        target, origin = descriptor(), source()
        adapter = T4Adapter(SourceRegistry((origin,)), worker,
                            local_snapshot_provider=lambda _: SubscriptionSnapshot(
                                origin.source_id, 1, (target,)))
        adapter._prewarm.delay = 0
        adapter._prewarm.remember(origin, target, {"list": [{
            "vod_play_from": "HD", "vod_play_url": "1$a#2$b",
        }]})
        req = lambda value: T4Request("play", origin.source_id, target.plugin_id,
                                     {"play": value, "flag": "HD"})
        try:
            await adapter.dispatch(req("a"))
            assert await asyncio.to_thread(entered.wait, 2)
            following = asyncio.create_task(adapter.dispatch(req("b")))
            await asyncio.sleep(0.01)
            assert len(worker.calls) == 1 and not following.done()
            release.set()
            assert (await following)["url"] == "https://cdn.example/warmed.mp4"
            assert len(worker.calls) == worker.warms == 1
        finally:
            release.set()
            await adapter.aclose()
            adapter.close()

    asyncio.run(scenario())


PYTHON_PLAYLIST_PLUGIN = """
from base.spider import Spider
class Spider(Spider):
    backend_parse = False
    def init(self, extend=""): self.count = 0
    def getName(self): return "Playlist fixture"
    def detailContent(self, ids):
        return {"list": [{"vod_id": ids[0], "vod_play_from": "HD",
                         "vod_play_url": "1$opaque-a#2$opaque-b#3$opaque-c"}]}
    def playerContent(self, flag, id, vipFlags):
        self.count += 1
        return {"url": "https://cdn.example/" + id + ".mp4", "count": self.count,
                "header": {"User-Agent": "playlist-fixture"}}
"""

JS_PLAYLIST_PLUGIN = """
let count = 0;
module.exports = {
  init: () => { count = 0; },
  getName: () => "Playlist fixture",
  detailContent: (ids) => ({list: [{vod_id: ids[0], vod_play_from: "HD",
    vod_play_url: "1$opaque-a#2$opaque-b#3$opaque-c"}]}),
  playerContent: (flag, id) => ({url: "https://cdn.example/" + id + ".mp4",
    count: ++count, header: {"User-Agent": "playlist-fixture"}})
};
"""


@pytest.mark.parametrize("kind", ["raw_python", "secspider", "javascript"])
def test_real_worker_playlist_prefetch_reuses_runtime_and_consumes_next_once(tmp_path, kind):
    if kind == "javascript" and not (shutil.which("node") or shutil.which("nodejs")):
        pytest.skip("JavaScript runtime is not installed")
    target = replace(descriptor(), plugin_type=kind, token="-", backend_api="")
    if kind == "secspider":
        package, loader, _, _ = build_secspider_fixture(tmp_path, PYTHON_PLAYLIST_PLUGIN)
        target = replace(target, source=package, loader=loader, raw=False)
    else:
        path = tmp_path / ("playlist.js" if kind == "javascript" else "playlist.py")
        path.write_text(JS_PLAYLIST_PLUGIN if kind == "javascript" else PYTHON_PLAYLIST_PLUGIN,
                        encoding="utf-8")
        target = replace(target, source="", loader=str(path))

    async def scenario():
        origin = source()
        pool = WorkerPool(WorkerConfig(request_timeout=10))
        invoker = AtvpWorkerInvoker(pool)
        adapter = T4Adapter(SourceRegistry((origin,)), invoker,
                            local_snapshot_provider=lambda _: SubscriptionSnapshot(
                                origin.source_id, 1, (target,)))
        adapter._prewarm.delay = 0
        req = lambda module, params: T4Request(module, origin.source_id, target.plugin_id, params)
        try:
            payload = await adapter.dispatch(req("detail", {"ids": "show"}))
            ids = [part.partition("$")[2] for part in payload["list"][0]["vod_play_url"].split("#")]
            pid = pool.session_identity(f"{origin.source_id}:{target.plugin_id}")
            assert (await adapter.dispatch(req("play", {"play": ids[0], "flag": "HD"})))["count"] == 1
            await adapter._prewarm.task
            assert len(adapter._prewarm.results) == 1
            assert pool.resource_summary()["workers"] == 1
            assert pool.session_identity(f"{origin.source_id}:{target.plugin_id}") == pid
            following = await adapter.dispatch(req("play", {"play": ids[1], "flag": "HD"}))
            assert following["url"] == "https://cdn.example/opaque-b.mp4"
            assert following["count"] == 2
            assert following["header"] == {"User-Agent": "playlist-fixture"}
            await adapter._prewarm.task
            assert (await adapter.dispatch(req("play", {"play": ids[2], "flag": "HD"})))["count"] == 3
        finally:
            await adapter.aclose()
            adapter.close()
            invoker.close()

    asyncio.run(scenario())


def test_pian_dan_navigation_preserves_real_plugin_next_episode(tmp_path):
    async def scenario():
        origin = source()
        target = descriptor()
        nav = replace(descriptor(), plugin_id="nav", api="csp_PianDan")

        class Worker(FakeWorker):
            def call(self, key, plugin, method, params):
                if method == "detail":
                    return {"list": [{
                        "vod_id": "show", "vod_play_from": "HD",
                        "vod_play_url": "1$a#2$b",
                    }]}
                return super().call(key, plugin, method, params)

            def prefetch(self, key, plugin, params):
                self.calls.append((key, "prefetch", params))
                return {"url": "https://cdn.example/next.mp4"}

        worker = Worker()
        adapter = T4Adapter(SourceRegistry((origin,)), worker,
                            local_snapshot_provider=lambda _: SubscriptionSnapshot(
                                origin.source_id, 2, (nav, target)))
        adapter._prewarm.delay = 0
        try:
            vod = (await adapter.dispatch(T4Request(
                "detail", origin.source_id, target.plugin_id, {"ids": "show"},
            )))["list"][0]
            navigation = adapter._navigation_result(vod, origin, nav, target)
            ids = [part.partition("$")[2] for part in navigation["vod_play_url"].split("#")]
            req = lambda value: T4Request("play", origin.source_id, nav.plugin_id,
                                         {"play": value, "flag": "ignored"})
            await adapter.dispatch(req(ids[0]))
            await adapter._prewarm.task
            assert worker.calls[-1][2]["id"] == "b"
            assert worker.calls[-1][2]["flag"] == "HD"
            assert (await adapter.dispatch(req(ids[1])))["url"] == "https://cdn.example/next.mp4"
            assert len(worker.calls) == 2
        finally:
            await adapter.aclose()
            adapter.close()

    asyncio.run(scenario())


def test_real_worker_proxy_created_during_prefetch_is_registered_and_rewritten(tmp_path):
    path = tmp_path / "proxy-playlist.py"
    path.write_text(PYTHON_PLAYLIST_PLUGIN.replace(
        '"https://cdn.example/" + id + ".mp4"',
        '(self.getProxyUrl() + "&episode=2" if id == "opaque-b" '
        'else "https://cdn.example/" + id + ".mp4")',
    ), encoding="utf-8")

    async def scenario():
        target = replace(descriptor(), source="", loader=str(path))
        origin = source()
        invoker = AtvpWorkerInvoker(WorkerPool(WorkerConfig(request_timeout=10)))
        adapter = T4Adapter(SourceRegistry((origin,)), invoker,
                            local_snapshot_provider=lambda _: SubscriptionSnapshot(
                                origin.source_id, 1, (target,)))
        adapter._prewarm.delay = 0
        req = lambda module, params: T4Request(
            module, origin.source_id, target.plugin_id, params, "https://gateway.example",
        )
        try:
            payload = await adapter.dispatch(req("detail", {"ids": "show"}))
            ids = [part.partition("$")[2] for part in payload["list"][0]["vod_play_url"].split("#")]
            await adapter.dispatch(req("play", {"play": ids[0], "flag": "HD"}))
            await adapter._prewarm.task
            result = await adapter.dispatch(req("play", {"play": ids[1], "flag": "HD"}))
            assert result["url"].startswith("https://gateway.example/t4/proxy/")
            assert "127.0.0.1" not in result["url"]
            assert result["count"] == 2
        finally:
            await adapter.aclose()
            adapter.close()
            invoker.close()

    asyncio.run(scenario())


def test_active_stateful_plugin_proxy_does_not_get_overwritten_by_speculative_play(tmp_path):
    path = tmp_path / "stateful.py"
    path.write_text(PYTHON_PLAYLIST_PLUGIN.replace(
        '"https://cdn.example/" + id + ".mp4"', "self.getProxyUrl()",
    ), encoding="utf-8")

    async def scenario():
        target = replace(descriptor(), source="", loader=str(path))
        origin = source()
        invoker = AtvpWorkerInvoker(WorkerPool(WorkerConfig(request_timeout=10)))
        adapter = T4Adapter(SourceRegistry((origin,)), invoker,
                            local_snapshot_provider=lambda _: SubscriptionSnapshot(
                                origin.source_id, 1, (target,)))
        adapter._prewarm.delay = 0
        req = lambda module, params: T4Request(
            module, origin.source_id, target.plugin_id, params, "https://gateway.example",
        )
        try:
            await adapter.dispatch(req("detail", {"ids": "show"}))
            result = await adapter.dispatch(req("play", {"play": "opaque-a", "flag": "HD"}))
            assert "/t4/proxy/" in result["url"]
            assert adapter._prewarm.task is None
            result = await adapter.dispatch(req("play", {"play": "opaque-b", "flag": "HD"}))
            assert result["count"] == 2
            assert adapter._prewarm.task is None
        finally:
            await adapter.aclose()
            adapter.close()
            invoker.close()

    asyncio.run(scenario())


def test_worker_restart_cannot_reuse_cached_urls_from_old_process(tmp_path):
    path = tmp_path / "playlist.py"
    path.write_text(PYTHON_PLAYLIST_PLUGIN, encoding="utf-8")

    async def scenario():
        target = replace(descriptor(), source="", loader=str(path))
        origin = source()
        pool = WorkerPool(WorkerConfig(request_timeout=10))
        invoker = AtvpWorkerInvoker(pool)
        adapter = T4Adapter(SourceRegistry((origin,)), invoker,
                            local_snapshot_provider=lambda _: SubscriptionSnapshot(
                                origin.source_id, 1, (target,)))
        adapter._prewarm.delay = 0
        req = lambda module, params: T4Request(module, origin.source_id, target.plugin_id, params)
        try:
            await adapter.dispatch(req("detail", {"ids": "show"}))
            await adapter.dispatch(req("play", {"play": "opaque-a", "flag": "HD"}))
            await adapter._prewarm.task
            assert adapter._prewarm.results
            await asyncio.to_thread(pool.restart, f"{origin.source_id}:{target.plugin_id}")
            result = await adapter.dispatch(req("play", {"play": "opaque-b", "flag": "HD"}))
            assert result["count"] == 1  # A live resolution in the new process, not count=2.
        finally:
            await adapter.aclose()
            adapter.close()
            invoker.close()

    asyncio.run(scenario())


def test_closing_waits_for_running_worker_resolution_without_orphaning_thread():
    async def scenario():
        warm = NextEpisodeWarmup(delay=0)
        warm.remember(source(), descriptor(), {"list": [{
            "vod_play_from": "HD", "vod_play_url": "1$a#2$b",
        }]})
        entered, release = threading.Event(), threading.Event()

        def blocking_resolver():
            entered.set()
            assert release.wait(3)
            return {"url": "https://cdn.example/next.mp4"}

        async def resolve(params):
            return await asyncio.to_thread(blocking_resolver)

        warm.schedule(source(), descriptor(), {"id": "a", "flag": "HD"}, "", None, resolve)
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(warm.aclose())
        await asyncio.sleep(0.01)
        assert not closing.done()
        release.set()
        await closing
        assert warm.task.done() and not warm.results

    asyncio.run(scenario())


@pytest.mark.parametrize("suffix", [".py", ".js"])
def test_registered_uploads_prefetch_same_line_and_delete_invalidates_results(tmp_path, suffix):
    if suffix == ".js" and not (shutil.which("node") or shutil.which("nodejs")):
        pytest.skip("JavaScript runtime is not installed")
    store = UploadedPluginStore(tmp_path)
    content = JS_PLAYLIST_PLUGIN if suffix == ".js" else PYTHON_PLAYLIST_PLUGIN
    target = store.register("playlist" + suffix, content.encode("utf-8"))

    async def scenario():
        origin = store.source_config
        invoker = AtvpWorkerInvoker(WorkerPool(WorkerConfig(request_timeout=10)))
        adapter = T4Adapter(SourceRegistry((origin,)), invoker,
                            local_snapshot_provider=store.snapshot_for)
        adapter._prewarm.delay = 0
        req = lambda module, params: T4Request(module, origin.source_id, target.plugin_id, params)
        try:
            await adapter.dispatch(req("detail", {"ids": "show"}))
            await adapter.dispatch(req("play", {"play": "opaque-a", "flag": "HD"}))
            await adapter._prewarm.task
            assert adapter._prewarm.results
            result = await adapter.dispatch(req("play", {"play": "opaque-b", "flag": "HD"}))
            assert result["count"] == 2
            await adapter._prewarm.task
            async with adapter.plugin_lock(origin.source_id, target.plugin_id):
                assert store.remove(target.plugin_id)
                await adapter.reset_plugin(origin.source_id, target.plugin_id)
            assert not adapter._prewarm.results and not adapter._prewarm.links
            with pytest.raises(ValueError, match="requested plugin was not found"):
                await adapter.dispatch(req("play", {"play": "opaque-c", "flag": "HD"}))
        finally:
            await adapter.aclose()
            adapter.close()
            invoker.close()

    asyncio.run(scenario())


def test_deleted_upload_is_not_resurrected_by_delayed_warmup(tmp_path):
    store = UploadedPluginStore(tmp_path)
    target = store.register("playlist.py", PYTHON_PLAYLIST_PLUGIN.encode("utf-8"))

    class Worker(FakeWorker):
        def prefetch(self, *args):
            raise AssertionError("deleted plugin must not be called")

    async def scenario():
        origin = store.source_config
        adapter = T4Adapter(SourceRegistry((origin,)), Worker(),
                            local_snapshot_provider=store.snapshot_for)
        adapter._prewarm.delay = 0.01
        try:
            adapter._prewarm.remember(origin, target, {"list": [{
                "vod_play_from": "HD", "vod_play_url": "1$a#2$b",
            }]})
            await adapter.dispatch(T4Request(
                "play", origin.source_id, target.plugin_id, {"play": "a", "flag": "HD"},
            ))
            assert adapter._prewarm.task is not None
            async with adapter.plugin_lock(origin.source_id, target.plugin_id):
                assert store.remove(target.plugin_id)
                await adapter.reset_plugin(origin.source_id, target.plugin_id)
            await adapter._prewarm.task
            assert not adapter._prewarm.results and not adapter._prewarm.links
        finally:
            await adapter.aclose()
            adapter.close()

    asyncio.run(scenario())
