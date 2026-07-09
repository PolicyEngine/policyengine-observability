from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Defaults for the generic queued-transport knobs; the queued destination
# imports these so a constructor call and an env-configured build can
# never disagree about what "default" means.
DEFAULT_LOG_QUEUE_MAXSIZE = 1000
DEFAULT_LOG_QUEUE_CLOSE_TIMEOUT_SECONDS = 2.0

DEFAULT_METRIC_ATTRIBUTE_KEYS = (
    "service.name",
    "service.role",
    "deployment.environment",
    "operation",
    "flavor",
    "route",
    "method",
    "endpoint",
    "status_code",
    "country_id",
    "backend",
    "requested_version",
    "resolved_channel",
    "auth_result",
    "segment",
    "event",
    "error_type",
    "model",
    "tool",
    "stop_reason",
    "iteration",
    "provider",
)


def bool_from_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"0", "false", "no", "off"}


def csv_from_env(name: str) -> tuple[str, ...]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return ()
    return tuple(part.strip() for part in raw_value.split(",") if part.strip())


def float_from_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


def int_from_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def default_environment() -> str:
    return (
        os.getenv("OBSERVABILITY_ENVIRONMENT")
        or os.getenv("DEPLOYMENT_ENVIRONMENT")
        or os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or "development"
    )


# A log profile is a named preset expanding to generic routing primitives
# (a destination-name tuple and a stdout-formatter name). Presets may name
# strategies; the expansion mechanism knows nothing about any backend.
LOG_PROFILE_PRESETS: dict[str, tuple[tuple[str, ...], str]] = {
    # Platforms whose logging agent ingests stdout (Cloud Run, GKE):
    # agent-native stdout only, fully synchronous, zero threads.
    "gcp-agent": (("stdout",), "google"),
    # Platforms with no ingesting agent (Modal): plain stdout as the
    # durable record plus queued direct Cloud Logging writes.
    "gcp-direct": (("stdout", "google_cloud_logging"), "plain"),
    # Local development and the kill switch: plain stdout, zero threads.
    "plain-sync": (("stdout",), "plain"),
}


def _detect_log_profile() -> str | None:
    platform = (os.getenv("OBSERVABILITY_PLATFORM") or "").strip().lower()
    if platform == "google_cloud_run":
        return "gcp-agent"
    if platform == "modal":
        return "gcp-direct"
    if os.getenv("K_SERVICE"):
        return "gcp-agent"
    if os.getenv("MODAL_ENVIRONMENT") or os.getenv("MODAL_TASK_ID"):
        return "gcp-direct"
    return None


def _missing_strategy_requirements(
    destination_names: Sequence[str],
    resolved_config: Mapping[str, Any],
) -> list[tuple[str, str]]:
    """(destination, config field) pairs a preset needs but lacks.

    Strategies declare their requirements at registration
    (``register_destination(required_config=...)``); this check knows
    nothing about any backend.
    """
    # Imported lazily: the destinations package imports this module, so
    # a module-level import here would be circular. By the time a config
    # is resolved the package (and its strategy registrations) is loaded.
    from .destinations.registry import destination_strategy

    missing: list[tuple[str, str]] = []
    for name in destination_names:
        strategy = destination_strategy(name)
        if strategy is None:
            continue
        for field in strategy.required_config:
            if not resolved_config.get(field):
                missing.append((name, field))
    return missing


def _resolve_log_profile(
    raw_profile: str,
    *,
    resolved_config: Mapping[str, Any],
) -> tuple[str, tuple[tuple[str, ...], str] | None, list[str]]:
    """Resolve a profile name to (name, preset-or-None, warnings).

    ``auto`` without a recognized platform marker resolves to no preset,
    so caller-supplied defaults keep applying. ``resolved_config``
    carries the already-resolved config values that registered
    strategies may declare as requirements.
    """
    warnings: list[str] = []
    # Canonical profile names are hyphenated; accept the same case,
    # whitespace, and hyphen/underscore variance as destination and
    # formatter names.
    profile = raw_profile.strip().lower().replace("_", "-")
    if profile == "auto":
        detected = _detect_log_profile()
        if detected is None:
            return "auto", None, warnings
        profile = detected
    preset = LOG_PROFILE_PRESETS.get(profile)
    if preset is None:
        warnings.append(
            f"Unknown OBSERVABILITY_LOG_PROFILE {raw_profile!r}; "
            "using plain-sync."
        )
        profile = "plain-sync"
        preset = LOG_PROFILE_PRESETS[profile]
    missing = _missing_strategy_requirements(preset[0], resolved_config)
    if missing:
        requirements = ", ".join(
            f"{field} (destination {name})" for name, field in missing
        )
        warnings.append(
            f"Log profile {profile} requires {requirements}; using plain-sync."
        )
        profile = "plain-sync"
        preset = LOG_PROFILE_PRESETS[profile]
    return profile, preset, warnings


@dataclass(frozen=True)
class ObservabilityConfig:
    service_name: str = "policyengine-service"
    service_role: str = "api"
    environment: str = "development"
    enabled: bool = True
    request_logs_enabled: bool = True
    log_raw_ip: bool = True
    log_level: int = logging.INFO
    otel_enabled: bool = True
    otlp_endpoint: str | None = None
    otlp_protocol: str = "grpc"
    span_prefix: str | None = None
    tracer_name: str | None = None
    meter_name: str | None = None
    shutdown_timeout_seconds: float = 3.0
    instrument_fastapi: bool = False
    instrument_httpx: bool = False
    metric_attribute_keys: tuple[str, ...] = DEFAULT_METRIC_ATTRIBUTE_KEYS
    log_destinations: tuple[str, ...] = ("stdout",)
    google_cloud_project: str | None = None
    google_cloud_log_name: str = "policyengine-observability"
    stdout_format: str = "plain"
    log_queue_maxsize: int = DEFAULT_LOG_QUEUE_MAXSIZE
    log_queue_close_timeout_seconds: float = (
        DEFAULT_LOG_QUEUE_CLOSE_TIMEOUT_SECONDS
    )
    log_profile: str = "auto"
    config_warnings: tuple[str, ...] = ()

    @classmethod
    def from_env(
        cls,
        *,
        service_name: str,
        service_role: str = "api",
        enabled_default: bool = True,
        span_prefix: str | None = None,
        instrument_fastapi: bool = False,
        instrument_httpx: bool = False,
        metric_attribute_keys: Sequence[str] | None = None,
        extra_metric_attribute_keys: Sequence[str] = (),
        default_log_destinations: Sequence[str] = ("stdout",),
    ) -> ObservabilityConfig:
        level_name = os.getenv("OBSERVABILITY_LOG_LEVEL", "INFO").upper()
        log_level = getattr(logging, level_name, logging.INFO)
        otlp_protocol = (
            os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL")
            or os.getenv("OBSERVABILITY_OTLP_PROTOCOL")
            or cls.otlp_protocol
        )
        env_metric_keys = csv_from_env("OBSERVABILITY_METRIC_ATTRIBUTE_KEYS")
        env_extra_metric_keys = csv_from_env(
            "OBSERVABILITY_EXTRA_METRIC_ATTRIBUTE_KEYS"
        )
        env_log_destinations = csv_from_env("OBSERVABILITY_LOG_DESTINATIONS")
        resolved_metric_keys = _dedupe(
            env_metric_keys
            or metric_attribute_keys
            or DEFAULT_METRIC_ATTRIBUTE_KEYS,
            (*extra_metric_attribute_keys, *env_extra_metric_keys),
        )
        google_cloud_project = (
            os.getenv("OBSERVABILITY_GOOGLE_CLOUD_PROJECT")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or os.getenv("GCP_PROJECT")
            or os.getenv("GCLOUD_PROJECT")
            or None
        )
        log_profile, preset, profile_warnings = _resolve_log_profile(
            os.getenv("OBSERVABILITY_LOG_PROFILE") or cls.log_profile,
            # The values strategies may declare via required_config;
            # extend as future fields become requirement candidates.
            resolved_config={"google_cloud_project": google_cloud_project},
        )
        profile_destinations, profile_stdout_format = preset or (None, None)
        # Explicit granular env vars override the profile's expansion;
        # the profile overrides caller-supplied defaults.
        resolved_log_destinations = _dedupe(
            env_log_destinations
            or profile_destinations
            or default_log_destinations
        )
        resolved_stdout_format = (
            os.getenv("OBSERVABILITY_STDOUT_FORMAT")
            or profile_stdout_format
            or cls.stdout_format
        )
        return cls(
            service_name=os.getenv("OBSERVABILITY_SERVICE_NAME")
            or os.getenv("OTEL_SERVICE_NAME")
            or service_name,
            service_role=service_role,
            environment=default_environment(),
            enabled=bool_from_env("OBSERVABILITY_ENABLED", enabled_default),
            request_logs_enabled=bool_from_env(
                "OBSERVABILITY_REQUEST_LOGS_ENABLED",
                True,
            ),
            log_raw_ip=bool_from_env("OBSERVABILITY_LOG_RAW_IP", True),
            log_level=log_level,
            otel_enabled=bool_from_env("OTEL_ENABLED", cls.otel_enabled),
            otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            otlp_protocol=otlp_protocol,
            span_prefix=span_prefix,
            tracer_name=os.getenv("OBSERVABILITY_TRACER_NAME"),
            meter_name=os.getenv("OBSERVABILITY_METER_NAME"),
            shutdown_timeout_seconds=float_from_env(
                "OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS",
                cls.shutdown_timeout_seconds,
            ),
            instrument_fastapi=bool_from_env(
                "OBSERVABILITY_INSTRUMENT_FASTAPI",
                instrument_fastapi,
            ),
            instrument_httpx=bool_from_env(
                "OBSERVABILITY_INSTRUMENT_HTTPX",
                instrument_httpx,
            ),
            metric_attribute_keys=resolved_metric_keys,
            log_destinations=resolved_log_destinations,
            google_cloud_project=google_cloud_project,
            google_cloud_log_name=(
                os.getenv("OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME")
                or cls.google_cloud_log_name
            ),
            stdout_format=resolved_stdout_format,
            log_queue_maxsize=int_from_env(
                "OBSERVABILITY_LOG_QUEUE_MAXSIZE",
                cls.log_queue_maxsize,
            ),
            log_queue_close_timeout_seconds=float_from_env(
                "OBSERVABILITY_LOG_QUEUE_CLOSE_TIMEOUT_SECONDS",
                cls.log_queue_close_timeout_seconds,
            ),
            log_profile=log_profile,
            config_warnings=tuple(profile_warnings),
        )


def _dedupe(
    base: Sequence[str],
    extra: Sequence[str] = (),
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*base, *extra)))
