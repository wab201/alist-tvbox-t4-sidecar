from sidecar.gateway.config import Settings


def test_default_settings_are_local_and_non_running() -> None:
    settings = Settings()

    assert settings.service_name == "t4-sidecar"
    assert settings.version == "0.2.1"
    assert settings.worker_status == "not_started"
    assert settings.worker_timeout == 15.0


def test_settings_read_public_environment_overrides(monkeypatch) -> None:
    monkeypatch.delenv("ALIST_TVBOX_SUBSCRIPTION_URL", raising=False)
    monkeypatch.setenv("T4_SERVICE_NAME", "custom-sidecar")
    monkeypatch.setenv("T4_VERSION", "9.9.9")
    monkeypatch.setenv("T4_WORKER_STATUS", "ready")
    monkeypatch.setenv("T4_WORKER_TIMEOUT", "3.5")

    settings = Settings.from_env()

    assert settings == Settings(
        service_name="custom-sidecar",
        version="0.2.1",
        worker_status="ready",
        worker_timeout=3.5,
    )


def test_settings_reads_at_subscription_url(monkeypatch) -> None:
    monkeypatch.setenv(
        "ALIST_TVBOX_SUBSCRIPTION_URL",
        "https://at.example/sub/token/Chajian",
    )

    settings = Settings.from_env()

    assert settings.subscription_url == "https://at.example/sub/token/Chajian"
