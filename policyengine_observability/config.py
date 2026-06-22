from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import logging
import os


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


def default_environment() -> str:
    return (
        os.getenv("OBSERVABILITY_ENVIRONMENT")
        or os.getenv("DEPLOYMENT_ENVIRONMENT")
        or os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or "development"
    )


@dataclass(frozen=True)
class ObservabilityConfig:
    service_name: str = "policyengine-service"
    service_role: str = "api"
    environment: str = "development"
    enabled: bool = True
    request_logs_enabled: bool = True
    log_raw_ip: bool = True
    log_level: int = logging.INFO
    otel_enabled: bool = False
    otlp_endpoint: str | None = None
    otlp_protocol: str = "grpc"
    span_prefix: str | None = None
    tracer_name: str | None = None
    meter_name: str | None = None
    shutdown_timeout_seconds: float = 3.0
    instrument_fastapi: bool = False
    instrument_httpx: bool = False
    metric_attribute_keys: tuple[str, ...] = DEFAULT_METRIC_ATTRIBUTE_KEYS

    @classmethod
    def from_env(
        cls,
        *,
        service_name: str,
        service_role: str = "api",
        enabled_default: bool = True,
        otel_enabled_default: bool = False,
        span_prefix: str | None = None,
        instrument_fastapi: bool = False,
        instrument_httpx: bool = False,
        metric_attribute_keys: Sequence[str] | None = None,
        extra_metric_attribute_keys: Sequence[str] = (),
    ) -> "ObservabilityConfig":
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
        resolved_metric_keys = _dedupe(
            env_metric_keys
            or metric_attribute_keys
            or DEFAULT_METRIC_ATTRIBUTE_KEYS,
            (*extra_metric_attribute_keys, *env_extra_metric_keys),
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
            otel_enabled=bool_from_env(
                "OTEL_ENABLED",
                bool_from_env(
                    "OBSERVABILITY_OTEL_ENABLED",
                    otel_enabled_default,
                ),
            ),
            otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            otlp_protocol=otlp_protocol,
            span_prefix=span_prefix,
            tracer_name=os.getenv("OBSERVABILITY_TRACER_NAME"),
            meter_name=os.getenv("OBSERVABILITY_METER_NAME"),
            shutdown_timeout_seconds=float_from_env(
                "OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS",
                3.0,
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
        )


def _dedupe(
    base: Sequence[str],
    extra: Sequence[str] = (),
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*base, *extra)))
