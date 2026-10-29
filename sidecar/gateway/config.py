from __future__ import annotations

import os
from dataclasses import dataclass
from sidecar import __version__


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration for the gateway process.

    Secrets and subscription material are intentionally not part of the health
    response. Local plugin storage is separate from the AT subscription.
    """

    service_name: str = "t4-sidecar"
    version: str = __version__
    worker_status: str = "not_started"
    worker_timeout: float = 15.0
    subscription_cache_ttl: float = 30.0
    plugin_dir: str = "/data/plugins"
    upload_max_bytes: int = 2_000_000
    source_config_file: str | None = None
    subscription_url: str | None = None
    log_file: str | None = None
    compatibility_report: str | None = None
    cloud_playback: str = "direct"
    at_internal_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            service_name=os.getenv("T4_SERVICE_NAME", "t4-sidecar"),
            version=__version__,
            worker_status=os.getenv("T4_WORKER_STATUS", "not_started"),
            worker_timeout=float(os.getenv("T4_WORKER_TIMEOUT", "15")),
            subscription_cache_ttl=float(
                os.getenv("T4_SUBSCRIPTION_CACHE_TTL", "30")
            ),
            plugin_dir=os.getenv("T4_PLUGIN_DIR", "/data/plugins"),
            upload_max_bytes=int(os.getenv("T4_UPLOAD_MAX_BYTES", "2000000")),
            source_config_file=os.getenv("T4_SOURCES_FILE") or None,
            subscription_url=os.getenv("ALIST_TVBOX_SUBSCRIPTION_URL") or None,
            log_file=os.getenv("T4_LOG_FILE") or None,
            compatibility_report=os.getenv("T4_COMPATIBILITY_REPORT") or None,
            cloud_playback=os.getenv("T4_CLOUD_PLAYBACK_MODE", "direct"),
            at_internal_url=os.getenv("ALIST_TVBOX_INTERNAL_URL") or None,
        )
