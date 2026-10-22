from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration for the gateway process.

    Secrets and subscription material are intentionally not part of the M1 health
    configuration. Later nodes add their own protected configuration boundaries.
    """

    service_name: str = "t4-sidecar"
    version: str = "0.1.0"
    worker_status: str = "not_started"
    worker_timeout: float = 15.0
    gateway_token: str | None = None
    source_config_file: str | None = None
    subscription_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            service_name=os.getenv("T4_SERVICE_NAME", "t4-sidecar"),
            version=os.getenv("T4_VERSION", "0.1.0"),
            worker_status=os.getenv("T4_WORKER_STATUS", "not_started"),
            worker_timeout=float(os.getenv("T4_WORKER_TIMEOUT", "15")),
            gateway_token=os.getenv("T4_GATEWAY_TOKEN") or None,
            source_config_file=os.getenv("T4_SOURCES_FILE") or None,
            subscription_url=os.getenv("ALIST_TVBOX_SUBSCRIPTION_URL") or None,
        )
