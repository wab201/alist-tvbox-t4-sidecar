import base64
import json
import io
import os
from pathlib import Path
import sys
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from sidecar.worker.atvp_runtime import AtvpRuntime
from sidecar.worker.client import WorkerConfig, WorkerPool, WorkerTimeoutError
from sidecar.worker.host import PluginHost


SOURCE = '''
from base.spider import Spider as Base
import os
class Spider(Base):
    def init(self, extend):
        self.count = 0
    def getName(self): return "resource fixture"
    def homeContent(self, filter):
        self.count += 1
        return {"list": [], "count": self.count, "setting": os.getenv("T4_RESOURCE_FIXTURE")}
    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "url": self.getProxyUrl()}
    def localProxy(self, params):
        return [200, "video/mp2t", b"retained-playback-state"]
'''


def extend_for(tmp_path: Path) -> str:
    path = tmp_path / "resource.py"
    path.write_text(SOURCE, encoding="utf-8")
    return base64.b64encode(json.dumps({"loader": str(path)}).encode()).decode()


def test_host_and_runtime_do_not_start_a_proxy_until_requested(tmp_path):
    runtime = AtvpRuntime()
    try:
        initialized = runtime.init(extend_for(tmp_path), site_key="fixture")
        assert "proxy_url" not in initialized
        assert runtime.proxy_endpoint is None
        assert runtime.home()["count"] == 1
        assert runtime._host._thread is None
        play = runtime.play("", "", [])
        assert runtime.proxy_endpoint is not None
        assert play["url"].startswith(runtime.proxy_endpoint)
        assert httpx.get(play["url"], trust_env=False).content == b"retained-playback-state"
    finally:
        runtime.close()


def test_proxy_listener_blocks_without_a_polling_timeout(monkeypatch):
    import selectors
    entered = threading.Event()
    select = selectors.DefaultSelector.select
    waits = []

    def tracked(self, timeout=None):
        waits.append(timeout)
        entered.set()
        return select(self, timeout)

    monkeypatch.setattr(selectors.DefaultSelector, "select", tracked)
    host = PluginHost(lambda _: [200, "text/plain", b"ok"], "fixture")
    assert host.started_endpoint is None
    host.endpoint
    assert entered.wait(2)
    host.close()
    assert waits and all(timeout is None for timeout in waits)
    assert not host._thread.is_alive()


@pytest.mark.parametrize("factory", [bytes, bytearray])
def test_buffered_proxy_range_does_not_copy_the_entire_plugin_payload(factory):
    payload = factory(b"0123456789")
    written, headers, statuses = [], {}, []
    handler = SimpleNamespace(
        path="/proxy", command="GET", headers={"Range": "bytes=2-4"},
        send_response=statuses.append, send_header=lambda key, value: headers.update({key: value}),
        end_headers=lambda: None, wfile=SimpleNamespace(write=written.append),
    )
    host = PluginHost(lambda _: [200, "video/mp2t", payload], "fixture")
    try:
        host._serve(handler)
    finally:
        host.close()
    assert statuses == [206]
    assert headers["Content-Range"] == "bytes 2-4/10" and headers["Content-Length"] == "3"
    assert len(written) == 1 and isinstance(written[0], memoryview)
    if factory is bytes:
        assert written[0].obj is payload
    else:
        payload[2:5] = b"XXX"
        assert isinstance(written[0].obj, bytes)
    assert written[0].tobytes() == b"234"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux forkserver/COW transport")
def test_forkserver_preserves_isolation_environment_proxy_and_timeout_recovery(tmp_path, monkeypatch):
    extend = extend_for(tmp_path)
    with WorkerPool(WorkerConfig(start_method="forkserver", request_timeout=5)) as pool:
        monkeypatch.setenv("T4_RESOURCE_FIXTURE", "first")
        pool.request("first", "init", {"extend": extend, "site_key": "first"})
        assert pool.proxy_endpoint("first") is None
        first = pool.request("first", "home")
        assert first["count"] == 1 and first["setting"] == "first"
        monkeypatch.setenv("T4_RESOURCE_FIXTURE", "second")
        pool.request("second", "init", {"extend": extend, "site_key": "second"})
        assert pool.request("second", "home")["setting"] == "second"
        assert pool.request("first", "home")["count"] == 2
        play = pool.request("first", "play")
        endpoint = pool.proxy_endpoint("first")
        assert endpoint is not None and play["url"].startswith(endpoint)
        assert httpx.get(play["url"], trust_env=False).content == b"retained-playback-state"
        old = pool.request("second", "inspect")
        with pytest.raises(WorkerTimeoutError):
            pool.request("second", "delay", {"seconds": 3}, timeout=.02)
        assert pool.request("second", "inspect")["pid"] != old["pid"]
        assert httpx.get(play["url"], trust_env=False).status_code == 200


@pytest.mark.skipif(sys.platform != "linux", reason="Linux forkserver/COW transport")
def test_parallel_forkserver_launches_release_process_descriptors():
    before = len(os.listdir("/proc/self/fd"))
    with WorkerPool(WorkerConfig(start_method="forkserver")) as pool:
        with ThreadPoolExecutor(max_workers=4) as threads:
            rows = list(threads.map(
                lambda key: pool.request(str(key), "inspect"), range(12),
            ))
        assert len({row["pid"] for row in rows}) == 12
        pool.close()
        assert not pool._sessions
    # One forkserver and its resource tracker keep a small fixed set of FDs.
    assert len(os.listdir("/proc/self/fd")) <= before + 6


@pytest.mark.skipif(sys.platform != "linux", reason="Linux forkserver process groups")
def test_crashed_worker_closes_stdin_and_releases_plugin_children(tmp_path):
    source = SOURCE + '''
    def homeContent(self, filter):
        import subprocess, sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        from pathlib import Path
        Path(__file__ + ".child").write_text(str(child.pid))
        os._exit(3)
'''
    path = tmp_path / "crash.py"
    path.write_text(source)
    extend = base64.b64encode(json.dumps({"loader": str(path)}).encode()).decode()
    with WorkerPool(WorkerConfig(start_method="forkserver", request_timeout=5)) as pool:
        pool.request("crash", "init", {"extend": extend})
        session = pool._sessions["crash"]
        stdin = session._process.stdin
        from sidecar.worker.client import WorkerError
        with pytest.raises(WorkerError):
            pool.request("crash", "home")
        child_pid = int(Path(str(path) + ".child").read_text())
        until = time.monotonic() + 2
        while time.monotonic() < until:
            child = Path(f"/proc/{child_pid}/stat")
            if not child.exists() or child.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(.01)
        else:
            pytest.fail("plugin child survived its worker")
        assert stdin.closed
        assert not session._reader.is_alive()
        assert not session._stderr_reader.is_alive()
        assert pool.request("crash", "ping") == {"status": "ready"}


def test_worker_idle_reclaim_keeps_plugin_state_and_proxy_address(tmp_path):
    with WorkerPool(WorkerConfig(request_timeout=10)) as pool:
        pool.request("fixture", "init", {"extend": extend_for(tmp_path), "site_key": "fixture"})
        original = pool.request("fixture", "inspect")
        assert pool.request("fixture", "home")["count"] == 1
        play = pool.request("fixture", "play")
        result = pool.request("fixture", "reclaim", {"idle_seconds": 0})
        assert result["reclaimed"] and result["trim_supported"] == (sys.platform == "linux")
        assert pool.request("fixture", "home")["count"] == 2
        assert pool.request("fixture", "inspect") == original
        assert httpx.get(play["url"], trust_env=False).content == b"retained-playback-state"
        assert pool.reclaim_idle(idle_seconds=0, interval=30) == 1
        assert pool.reclaim_idle(idle_seconds=0, interval=30) == 0
        # Background replies must never become the next business response.
        assert pool.request("fixture", "home")["count"] == 3


def test_worker_reclaim_skips_live_media_and_resumes_after_the_stream(tmp_path):
    source = SOURCE + '''
    def localProxy(self, params):
        import time
        def chunks():
            for _ in range(32):
                time.sleep(.02)
                yield b"media" * 8192
        return [200, "application/octet-stream", chunks()]
'''
    path = tmp_path / "streaming.py"
    path.write_text(source)
    extend = base64.b64encode(json.dumps({"loader": str(path)}).encode()).decode()
    with WorkerPool(WorkerConfig(request_timeout=10)) as pool:
        pool.request("stream", "init", {"extend": extend})
        play = pool.request("stream", "play")
        with httpx.Client(trust_env=False) as client:
            with client.stream("GET", play["url"]) as response:
                chunks = response.iter_bytes()
                first = next(chunks)
                assert pool.request("stream", "reclaim", {"idle_seconds": 0})["reason"] == "active"
                content = first + b"".join(chunks)
            assert content == b"media" * 8192 * 32
        deadline = time.monotonic() + 2
        while not pool.request("stream", "reclaim", {"idle_seconds": 0})["reclaimed"]:
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert pool.request("stream", "home")["count"] == 1


def test_host_streaming_lifecycle_blocks_collection_until_body_is_closed():
    entered, finish, closed = threading.Event(), threading.Event(), threading.Event()

    class Stream:
        def __iter__(self):
            entered.set()
            assert finish.wait(5)
            yield b"media"

        def close(self):
            closed.set()

    host = PluginHost(lambda _: [200, "application/octet-stream", Stream()], "fixture")
    try:
        endpoint = host.endpoint
        with ThreadPoolExecutor(max_workers=1) as threads:
            request = threads.submit(lambda: httpx.get(endpoint, trust_env=False))
            try:
                assert entered.wait(3)
                assert host.memory.reclaim(idle_seconds=0)["reason"] == "active"
            finally:
                finish.set()
            assert request.result(timeout=3).content == b"media"
        assert closed.is_set()
        assert host.memory.reclaim(idle_seconds=0)["reclaimed"]
    finally:
        host.close()


def test_background_reclaim_does_not_wait_for_a_busy_rpc(tmp_path):
    with WorkerPool(WorkerConfig(request_timeout=10)) as pool:
        pool.request("fixture", "init", {"extend": extend_for(tmp_path)})
        with ThreadPoolExecutor(max_workers=1) as threads:
            future = threads.submit(pool.request, "fixture", "delay", {"seconds": .3})
            session = pool._sessions["fixture"]
            until = time.monotonic() + 2
            while not session._lock.locked() and time.monotonic() < until:
                time.sleep(.001)
            started = time.monotonic()
            assert pool.reclaim_idle(idle_seconds=0) == 0
            assert time.monotonic() - started < .1
            assert future.result(timeout=3) == {"status": "complete"}


def test_idle_reclaim_schedules_bounded_batches_instead_of_every_worker_at_once():
    class Session:
        def __init__(self):
            self.scheduled = False

        def reclaim_idle(self, **kwargs):
            if self.scheduled:
                return False
            self.scheduled = True
            return True

    pool = WorkerPool()
    pool._sessions = {str(index): Session() for index in range(20)}
    assert pool.reclaim_idle() == 8
    assert pool.reclaim_idle() == 8
    assert pool.reclaim_idle() == 4
    assert pool.reclaim_idle() == 0


def test_rpc_loop_releases_response_before_waiting_for_the_next_line(monkeypatch):
    from sidecar.worker import process

    class Response(dict):
        pass

    references = []

    def handle(request):
        response = Response(id=request["id"], ok=True, result={"large": "x" * 1024})
        references.append(weakref.ref(response))
        return response

    def lines():
        yield '{"id": "first", "method": "ping"}\n'
        assert references[0]() is None

    monkeypatch.setattr(process, "_handle", handle)
    monkeypatch.setattr(sys, "stdin", lines())
    output = io.StringIO()
    assert process._main(output) == 0
    assert json.loads(output.getvalue())["id"] == "first"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux allocator RSS accounting")
def test_idle_release_returns_freed_heap_and_keeps_live_objects():
    import gc
    import weakref
    from sidecar.resources import release_unused_memory

    class Cycle:
        pass

    gc.disable()
    try:
        item = Cycle()
        item.payload = bytearray(32 * 1024 * 1024)
        item.self = item
        retained = {"play_context": "episode-2", "buffer": bytearray(1024)}
        reference = weakref.ref(item)
        before = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        del item
        result = release_unused_memory()
        after = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        assert result["trim_supported"] and reference() is None
        assert before - after >= 24 * 1024 * 1024
        assert retained["play_context"] == "episode-2" and len(retained["buffer"]) == 1024
    finally:
        gc.enable()
