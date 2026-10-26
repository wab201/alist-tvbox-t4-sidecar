import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event

import pytest

from sidecar.at.subscription import SubscriptionParseError
from sidecar.gateway.source_registry import SourceRegistry
from sidecar.gateway.uploads import UploadedPluginStore
from sidecar.t4.adapter import AtvpWorkerInvoker, T4Adapter, T4Request
from tests.t4.test_adapter import descriptor


def test_updated_subscription_descriptor_reinitializes_worker():
    class Pool:
        def __init__(self):
            self.calls = []
            self.restarts = []

        def request(self, key, method, params):
            self.calls.append((key, method, params))
            return {"list": []}

        def restart(self, key):
            self.restarts.append(key)

    pool = Pool()
    invoker = AtvpWorkerInvoker(pool)
    plugin = descriptor()
    invoker.call("source:demo", plugin, "home", {})
    invoker.call("source:demo", plugin, "home", {})
    invoker.call("source:demo", replace(plugin, source="https://at.example/new.py"), "home", {})
    assert [method for _, method, _ in pool.calls] == ["init", "home", "home", "init", "home"]
    assert pool.restarts == ["source:demo"]
    invoker.reset("source:demo")
    invoker.call("source:demo", plugin, "home", {})
    assert [method for _, method, _ in pool.calls][-2:] == ["init", "home"]


def test_independent_plugin_initializations_do_not_block_each_other():
    barrier = Barrier(2)

    class Pool:
        def request(self, key, method, params):
            if method == "init":
                barrier.wait(timeout=3)
            return {"list": []}

    invoker = AtvpWorkerInvoker(Pool())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(invoker.call, key, descriptor(), "home", {})
            for key in ("source:first", "source:second")
        ]
        assert [future.result(timeout=4) for future in futures] == [{"list": []}, {"list": []}]


def test_failed_updated_init_does_not_reuse_old_initialized_state():
    class Pool:
        def __init__(self):
            self.fail = False
            self.init_count = 0

        def request(self, key, method, params):
            if method == "init":
                self.init_count += 1
                if self.fail:
                    raise RuntimeError("bad updated plugin")
            return {"list": []}

        def restart(self, key):
            pass

    pool = Pool()
    invoker = AtvpWorkerInvoker(pool)
    plugin = descriptor()
    invoker.call("source:demo", plugin, "home", {})
    pool.fail = True
    with pytest.raises(RuntimeError):
        invoker.call("source:demo", replace(plugin, data="new"), "home", {})
    pool.fail = False
    invoker.call("source:demo", plugin, "home", {})
    assert pool.init_count == 3


def test_delete_waits_for_active_call_and_queued_call_cannot_respawn_plugin(tmp_path):
    started, finish = Event(), Event()

    class Worker:
        calls = 0
        resets = 0

        def call(self, key, plugin, method, params):
            self.calls += 1
            started.set()
            assert finish.wait(timeout=3)
            return {"list": []}

        def reset(self, key):
            self.resets += 1

    store = UploadedPluginStore(tmp_path)
    plugin = store.register("demo.py", b"class Spider: pass")
    worker = Worker()
    source_id = store.source_config.source_id
    adapter = T4Adapter(
        SourceRegistry((store.source_config,)), worker,
        local_snapshot_provider=store.snapshot_for,
    )

    async def run():
        request = T4Request("home", source_id, plugin.plugin_id, {})
        active = asyncio.create_task(adapter.dispatch(request))
        assert await asyncio.to_thread(started.wait, 2)

        async def remove():
            async with adapter.plugin_lock(source_id, plugin.plugin_id):
                assert store.remove(plugin.plugin_id)
                await adapter.reset_plugin(source_id, plugin.plugin_id)

        deletion = asyncio.create_task(remove())
        await asyncio.sleep(0)
        queued = asyncio.create_task(adapter.dispatch(request))
        await asyncio.sleep(0)
        assert not deletion.done()
        finish.set()
        await active
        await deletion
        with pytest.raises(SubscriptionParseError):
            await queued

    asyncio.run(run())
    assert worker.calls == 1
    assert worker.resets == 1
