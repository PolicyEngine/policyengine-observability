from __future__ import annotations

import json
import logging

import pytest

from policyengine_observability.config import ObservabilityConfig
from policyengine_observability.destinations.manager import (
    LogDestinationManager,
)
from policyengine_observability.destinations.queued import (
    QueuedLogDestination,
)

PLATFORM_MARKERS = (
    "OBSERVABILITY_LOG_PROFILE",
    "OBSERVABILITY_PLATFORM",
    "K_SERVICE",
    "MODAL_ENVIRONMENT",
    "MODAL_TASK_ID",
    "OBSERVABILITY_LOG_DESTINATIONS",
    "OBSERVABILITY_STDOUT_FORMAT",
    "OBSERVABILITY_GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "GCP_PROJECT",
    "GCLOUD_PROJECT",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in PLATFORM_MARKERS:
        monkeypatch.delenv(name, raising=False)


def _from_env(monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return ObservabilityConfig.from_env(service_name="svc")


def test_explicit_gcp_agent_profile(monkeypatch) -> None:
    config = _from_env(monkeypatch, OBSERVABILITY_LOG_PROFILE="gcp-agent")

    assert config.log_profile == "gcp-agent"
    assert config.log_destinations == ("stdout",)
    assert config.stdout_format == "google"
    assert config.config_warnings == ()


def test_explicit_gcp_direct_profile_with_project(monkeypatch) -> None:
    config = _from_env(
        monkeypatch,
        OBSERVABILITY_LOG_PROFILE="gcp-direct",
        OBSERVABILITY_GOOGLE_CLOUD_PROJECT="proj",
    )

    assert config.log_profile == "gcp-direct"
    assert config.log_destinations == ("stdout", "google_cloud_logging")
    assert config.stdout_format == "plain"
    assert config.config_warnings == ()


def test_gcp_direct_without_project_downgrades_with_warning(
    monkeypatch,
) -> None:
    config = _from_env(monkeypatch, OBSERVABILITY_LOG_PROFILE="gcp-direct")

    assert config.log_profile == "plain-sync"
    assert config.log_destinations == ("stdout",)
    assert config.stdout_format == "plain"
    assert len(config.config_warnings) == 1
    assert "Google Cloud project" in config.config_warnings[0]


def test_explicit_plain_sync_profile_is_kill_switch(monkeypatch) -> None:
    config = _from_env(
        monkeypatch,
        OBSERVABILITY_LOG_PROFILE="plain-sync",
        OBSERVABILITY_PLATFORM="modal",
        OBSERVABILITY_GOOGLE_CLOUD_PROJECT="proj",
    )

    assert config.log_profile == "plain-sync"
    assert config.log_destinations == ("stdout",)
    assert config.stdout_format == "plain"


def test_unknown_profile_falls_back_to_plain_sync_with_warning(
    monkeypatch,
) -> None:
    config = _from_env(monkeypatch, OBSERVABILITY_LOG_PROFILE="gcp-agnet")

    assert config.log_profile == "plain-sync"
    assert config.log_destinations == ("stdout",)
    assert any("gcp-agnet" in w for w in config.config_warnings)


def test_auto_detects_cloud_run_via_observability_platform(
    monkeypatch,
) -> None:
    config = _from_env(monkeypatch, OBSERVABILITY_PLATFORM="google_cloud_run")

    assert config.log_profile == "gcp-agent"
    assert config.stdout_format == "google"


def test_auto_detects_modal_via_observability_platform(monkeypatch) -> None:
    config = _from_env(
        monkeypatch,
        OBSERVABILITY_PLATFORM="modal",
        OBSERVABILITY_GOOGLE_CLOUD_PROJECT="proj",
    )

    assert config.log_profile == "gcp-direct"
    assert config.log_destinations == ("stdout", "google_cloud_logging")


def test_auto_detects_cloud_run_via_k_service(monkeypatch) -> None:
    config = _from_env(monkeypatch, K_SERVICE="household-api")

    assert config.log_profile == "gcp-agent"


def test_auto_detects_modal_via_task_marker(monkeypatch) -> None:
    config = _from_env(
        monkeypatch,
        MODAL_TASK_ID="ta-123",
        OBSERVABILITY_GOOGLE_CLOUD_PROJECT="proj",
    )

    assert config.log_profile == "gcp-direct"


def test_observability_platform_beats_generic_markers(monkeypatch) -> None:
    config = _from_env(
        monkeypatch,
        OBSERVABILITY_PLATFORM="google_cloud_run",
        MODAL_TASK_ID="ta-123",
    )

    assert config.log_profile == "gcp-agent"


def test_auto_without_markers_preserves_caller_defaults(monkeypatch) -> None:
    config = ObservabilityConfig.from_env(
        service_name="svc",
        default_log_destinations=("stdout", "custom"),
    )

    assert config.log_profile == "auto"
    assert config.log_destinations == ("stdout", "custom")
    assert config.stdout_format == "plain"
    assert config.config_warnings == ()


def test_explicit_destination_env_overrides_profile(monkeypatch) -> None:
    config = _from_env(
        monkeypatch,
        OBSERVABILITY_LOG_PROFILE="gcp-agent",
        OBSERVABILITY_LOG_DESTINATIONS="stdout,google_cloud_logging",
        OBSERVABILITY_GOOGLE_CLOUD_PROJECT="proj",
    )

    assert config.log_destinations == ("stdout", "google_cloud_logging")
    # The profile's other half still applies.
    assert config.stdout_format == "google"


def test_explicit_stdout_format_env_overrides_profile(monkeypatch) -> None:
    config = _from_env(
        monkeypatch,
        OBSERVABILITY_LOG_PROFILE="gcp-agent",
        OBSERVABILITY_STDOUT_FORMAT="plain",
    )

    assert config.stdout_format == "plain"
    assert config.log_destinations == ("stdout",)


def _configured_manager(config):
    failures = []
    manager = LogDestinationManager(
        config=config,
        loggers={"event": logging.getLogger("test-profiles")},
        serializer=json.dumps,
        on_failure=lambda operation, exc, **fields: failures.append(
            (operation, str(exc))
        ),
    )
    manager.configure()
    return manager, failures


def test_sync_profiles_build_no_queued_destinations(monkeypatch) -> None:
    for profile in ("gcp-agent", "plain-sync"):
        config = _from_env(monkeypatch, OBSERVABILITY_LOG_PROFILE=profile)
        manager, failures = _configured_manager(config)

        assert not any(
            isinstance(destination, QueuedLogDestination)
            for destination in manager.destinations
        )
        assert failures == []


def test_manager_reports_profile_warnings_once(monkeypatch) -> None:
    config = _from_env(monkeypatch, OBSERVABILITY_LOG_PROFILE="bogus")
    manager, failures = _configured_manager(config)

    warnings = [
        message
        for operation, message in failures
        if operation == "logging.profile_config"
    ]
    assert len(warnings) == 1
    assert "bogus" in warnings[0]
    manager.close()


def test_gcp_direct_profile_builds_queued_google(monkeypatch) -> None:
    from policyengine_observability.destinations import (
        google_cloud_logging as google_module,
    )

    class StubGoogle:
        name = "google_cloud_logging"

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def emit(self, payload, *, log_type, severity, timestamp=None):
            pass

    monkeypatch.setattr(
        google_module, "GoogleCloudLoggingDestination", StubGoogle
    )
    config = _from_env(
        monkeypatch,
        OBSERVABILITY_LOG_PROFILE="gcp-direct",
        OBSERVABILITY_GOOGLE_CLOUD_PROJECT="proj",
    )
    manager, failures = _configured_manager(config)

    stdout_destination, queued = manager.destinations
    assert isinstance(queued, QueuedLogDestination)
    assert isinstance(queued.inner, StubGoogle)
    assert queued.inner.kwargs["project"] == "proj"
    assert failures == []
    manager.close()
