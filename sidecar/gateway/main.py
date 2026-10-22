from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from sidecar.gateway.config import Settings
from sidecar.gateway.routes.health import build_health_router
from sidecar.gateway.routes.sources import build_sources_router
from sidecar.gateway.routes.subscriptions import (
    build_subscriptions_router,
    SubscriptionFetcher,
)
from sidecar.gateway.source_registry import SourceConfig, SourceRegistry
from sidecar.gateway.routes.t4 import build_t4_router
from sidecar.gateway.routes.web import build_web_router
from sidecar.at.native import NativeAtClient, NativeAtInvoker
from sidecar.t4.adapter import AtvpWorkerInvoker, T4Adapter
from sidecar.worker.client import WorkerConfig, WorkerPool


def create_app(
    settings: Settings | None = None,
    source_registry: SourceRegistry | None = None,
    subscription_fetcher: SubscriptionFetcher | None = None,
    t4_adapter: T4Adapter | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings.from_env()
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
                    ),
                )
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
            AtvpWorkerInvoker(worker_pool),
            subscription_fetcher=subscription_fetcher,
            native_invoker=NativeAtInvoker(native_client),
        )
    else:
        runtime_adapter = t4_adapter
    if worker_pool is not None:
        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            try:
                yield
            finally:
                worker_pool.close()
                if native_client is not None:
                    native_client.close()
    else:
        lifespan = None

    app = FastAPI(
        title="AList-TVBox T4 Sidecar",
        version=runtime_settings.version,
        docs_url="/t4/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.include_router(build_health_router(runtime_settings))
    app.include_router(build_sources_router(runtime_settings, runtime_sources))
    app.include_router(
        build_subscriptions_router(
            runtime_settings,
            runtime_sources,
            subscription_fetcher,
        )
    )
    app.include_router(build_t4_router(runtime_settings, runtime_sources, runtime_adapter))
    app.include_router(build_web_router())
    return app


app = create_app()
