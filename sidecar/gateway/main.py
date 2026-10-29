from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import httpx

from sidecar.at.subscription import CachedSubscriptionResolver
from sidecar.gateway.config import Settings
from sidecar.gateway.routes.health import build_health_router
from sidecar.gateway.routes.sources import build_sources_router
from sidecar.gateway.routes.subscriptions import (
    build_subscriptions_router,
    SubscriptionFetcher,
)
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.gateway.playback_settings import PlaybackSettingsStore
from sidecar.gateway.routes.t4 import build_t4_router
from sidecar.gateway.routes.web import build_web_router
from sidecar.gateway.routes.fm import build_fm_router
from sidecar.gateway.routes.client_config import build_client_config_router
from sidecar.gateway.routes.uploads import build_uploads_router
from sidecar.gateway.uploads import UploadedPluginStore
from sidecar.at.native import NativeAtClient, NativeAtInvoker
from sidecar.t4.adapter import AtvpWorkerInvoker, T4Adapter
from sidecar.worker.client import WorkerConfig, WorkerPool
from sidecar.diagnostics import TraceMiddleware, TraceStore
from sidecar.playback import PlaybackBridge
from sidecar.gateway.routes.diagnostics import build_diagnostics_router
from sidecar.compatibility.runner import CompatibilityRunner
from sidecar.gateway.routes.compatibility import build_compatibility_router
from sidecar.gateway.routes.plugin_proxy import build_plugin_proxy_router
from sidecar.plugin_proxy import PluginProxyRegistry
from sidecar.resources import IdleMemory, ResourceActivityMiddleware


def create_app(
    settings: Settings | None = None,
    source_registry: SourceRegistry | None = None,
    subscription_fetcher: SubscriptionFetcher | None = None,
    t4_adapter: T4Adapter | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings.from_env()
    trace_store = TraceStore(runtime_settings.log_file)
    proxies = PluginProxyRegistry()
    playback = PlaybackBridge(proxy_registry=proxies)
    proxy_client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(25, connect=3))
    if source_registry is not None:
        runtime_sources = source_registry
    else:
        try:
            runtime_sources = SourceRegistry.from_file(
                runtime_settings.source_config_file
            )
        except FileNotFoundError:
            if not runtime_settings.subscription_url:
                raise
            runtime_sources = SourceRegistry()
        if not runtime_sources.public_metadata() and runtime_settings.subscription_url:
            runtime_sources = SourceRegistry(
                (
                    SourceConfig(
                        source_id="env-at",
                        display_name="AT environment subscription",
                        status="configured",
                        capabilities=("subscription", "t4"),
                        subscription_url=runtime_settings.subscription_url,
                        cloud_playback=runtime_settings.cloud_playback,
                        at_internal_url=runtime_settings.at_internal_url,
                    ),
                )
            )
    upload_store = UploadedPluginStore(
        runtime_settings.plugin_dir,
        max_bytes=runtime_settings.upload_max_bytes,
    )
    if upload_store.has_plugins():
        runtime_sources.upsert(upload_store.source_config)
    playback_settings = PlaybackSettingsStore(
        runtime_sources, Path(runtime_settings.plugin_dir) / "playback.json",
    )
    subscription_resolver = CachedSubscriptionResolver(
        subscription_fetcher,
        ttl=runtime_settings.subscription_cache_ttl,
    )
    worker_pool: WorkerPool | None = None
    native_client: NativeAtClient | None = None
    if t4_adapter is None:
        worker_pool = WorkerPool(
            WorkerConfig(request_timeout=runtime_settings.worker_timeout)
        )
        native_client = NativeAtClient(timeout=runtime_settings.worker_timeout)
        runtime_adapter = T4Adapter(
            runtime_sources,
            AtvpWorkerInvoker(worker_pool, proxies),
            subscription_fetcher=subscription_fetcher,
            subscription_resolver=subscription_resolver.resolve,
            local_snapshot_provider=upload_store.snapshot_for,
            native_invoker=NativeAtInvoker(native_client),
            playback_bridge=playback,
        )
    else:
        runtime_adapter = t4_adapter
    # Gate workers are isolated from interactive player sessions.
    gate_pool = WorkerPool(WorkerConfig(request_timeout=runtime_settings.worker_timeout))
    gate_invoker = AtvpWorkerInvoker(gate_pool, proxies)
    gate_snapshots = {}

    async def frozen_gate_snapshot(source):
        return gate_snapshots.get(source.source_id) or await subscription_resolver.resolve(source)

    gate_adapter = T4Adapter(
        runtime_sources, gate_invoker,
        subscription_resolver=frozen_gate_snapshot,
        local_snapshot_provider=upload_store.snapshot_for,
        native_invoker=NativeAtInvoker(native_client) if native_client is not None else None,
        playback_bridge=playback,
    ) if t4_adapter is None else t4_adapter

    async def gate_snapshot(source):
        snapshot = upload_store.snapshot_for(source.source_id) or await subscription_resolver.refresh(source)
        gate_snapshots[source.source_id] = snapshot
        return snapshot

    compatibility = CompatibilityRunner(
        runtime_sources, gate_snapshot, gate_adapter, playback, trace_store,
        report_path=runtime_settings.compatibility_report,
        finish_runtime=gate_invoker.close,
        release_plugin=gate_adapter.reset_plugin if isinstance(gate_adapter, T4Adapter) else None,
        proxy_registry=proxies,
    )
    memory = IdleMemory()
    maintenance_stop = asyncio.Event()
    adapters = (runtime_adapter,) if gate_adapter is runtime_adapter else (runtime_adapter, gate_adapter)

    def reclaim_resources():
        for pool in (worker_pool, gate_pool):
            if pool is not None:
                pool.reclaim_idle()

        def cleanup():
            count = playback.prune_expired()
            for adapter in adapters:
                if isinstance(adapter, T4Adapter):
                    count += adapter.release_memory()
            return count

        result = memory.reclaim(cleanup=cleanup)
        if result["reclaimed"]:
            trace_store.emit(
                "gateway.memory_reclaimed", count=result["released_entries"],
                reason="trimmed" if result["trimmed"] else "collected",
            )

    async def memory_maintenance():
        while True:
            try:
                await asyncio.wait_for(maintenance_stop.wait(), timeout=15)
                return
            except TimeoutError:
                pass
            try:
                await asyncio.to_thread(reclaim_resources)
            except Exception as exc:
                trace_store.emit("memory.reclaim_failed", error=type(exc).__name__)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        maintenance = asyncio.create_task(memory_maintenance())
        try:
            yield
        finally:
            maintenance_stop.set()
            await maintenance
            await compatibility.close()
            if worker_pool is not None:
                worker_pool.close()
            gate_invoker.close()
            for adapter in adapters:
                if isinstance(adapter, T4Adapter):
                    adapter.close()
            if native_client is not None:
                native_client.close()
            await playback.aclose()
            playback.close()
            await proxy_client.aclose()
            trace_store.close()

    app = FastAPI(
        title="AList-TVBox T4 Sidecar",
        version=runtime_settings.version,
        docs_url="/t4/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.traces = trace_store
    app.state.playback = playback
    app.state.compatibility = compatibility
    app.state.reclaim_resources = reclaim_resources
    app.state.memory = memory
    app.include_router(build_plugin_proxy_router(proxies, proxy_client))
    app.add_middleware(TraceMiddleware, store=trace_store)
    app.add_middleware(ResourceActivityMiddleware, memory=memory)
    app.include_router(build_diagnostics_router(trace_store, playback, runtime_adapter))
    app.include_router(build_compatibility_router(compatibility))
    app.include_router(build_health_router(runtime_settings))
    app.include_router(build_sources_router(runtime_sources, playback_settings))
    app.include_router(
        build_client_config_router(
            runtime_sources,
            subscription_resolver=subscription_resolver.resolve,
            local_snapshot_provider=upload_store.snapshot_for,
        )
    )
    app.include_router(
        build_subscriptions_router(
            runtime_sources,
            subscription_fetcher,
            subscription_resolver=subscription_resolver.resolve,
            subscription_refresher=subscription_resolver.refresh,
            local_snapshot_provider=upload_store.snapshot_for,
        )
    )
    app.include_router(
        build_uploads_router(runtime_settings, runtime_sources, upload_store, runtime_adapter)
    )
    app.include_router(build_t4_router(runtime_sources, runtime_adapter, playback))
    app.include_router(
        build_fm_router(runtime_sources, runtime_adapter, playback)
    )
    app.include_router(build_web_router())
    app.mount(
        "/t4/assets",
        StaticFiles(directory=Path(__file__).parents[1] / "web" / "assets"),
        name="web-assets",
    )
    return app


app = create_app()
