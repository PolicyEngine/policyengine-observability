from __future__ import annotations

import math
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .config import ObservabilityConfig

SCHEMA_VERSION = "policyengine.observability.v2"

_PROHIBITED_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "request_body",
    "response_body",
    "household",
    "person",
    "reform",
    "payload",
    "raw_ip",
    "client_ip",
    "prompt",
    "model_response",
)


def normalize_attributes(
    values: Mapping[str, Any] | None,
    config: ObservabilityConfig,
    *,
    allowed_keys: frozenset[str] | None = None,
) -> tuple[dict[str, str | int | float | bool], int]:
    normalized: dict[str, str | int | float | bool] = {}
    omitted = 0
    for raw_key, value in (values or {}).items():
        key = str(raw_key).strip()
        if (
            not key
            or len(normalized) >= config.limits.max_attributes
            or _prohibited_key(key)
            or (allowed_keys is not None and key not in allowed_keys)
        ):
            omitted += 1
            continue
        scalar = _normalize_scalar(value, config)
        if scalar is None:
            omitted += 1
            continue
        normalized[key] = scalar
    return normalized, omitted


def build_record(
    config: ObservabilityConfig,
    *,
    severity: str,
    event_name: str | None = None,
    message: str | None = None,
    context: Mapping[str, Any] | None = None,
    attributes: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "severity": severity.upper(),
        "service.name": _limit(config.service.name, config),
        "service.namespace": _limit(config.service.namespace, config),
        "service.version": _limit(config.service.version, config),
        "service.role": _limit(config.service.role, config),
        "deployment.environment.name": _limit(
            config.deployment.environment, config
        ),
        "cloud.platform": config.deployment.platform,
    }
    if config.deployment.region:
        record["cloud.region"] = _limit(config.deployment.region, config)
    if config.deployment.instance_id:
        record["service.instance.id"] = _limit(
            config.deployment.instance_id, config
        )
    if event_name:
        record["event.name"] = _limit(event_name, config)
    if message:
        record["message"] = _redact(message, config)[
            : config.limits.max_string_length
        ]

    for key, value in (context or {}).items():
        if value is not None:
            record[str(key)] = value

    safe_attributes, omitted = normalize_attributes(
        attributes,
        config,
        allowed_keys=config.application_attribute_keys
        | config.dispatch_attribute_keys,
    )
    if safe_attributes:
        record["attributes"] = safe_attributes
    if omitted:
        record["attributes.omitted_count"] = omitted

    if error is not None:
        record.update(error_fields(error, config))

    return record


def metric_attributes(
    values: Mapping[str, Any], config: ObservabilityConfig
) -> dict[str, str | int | float | bool]:
    normalized, _ = normalize_attributes(
        values,
        config,
        allowed_keys=config.metric_attribute_keys,
    )
    return normalized


def error_fields(
    error: BaseException, config: ObservabilityConfig
) -> dict[str, Any]:
    try:
        message = str(error)
    except Exception:
        message = "<unprintable>"
    try:
        stack = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
    except Exception:
        stack = ""
    return {
        "error.type": type(error).__name__,
        "error.message": _redact(message, config)[
            : config.limits.max_error_message_length
        ],
        "error.stack": _redact(stack, config)[
            : config.limits.max_stack_length
        ],
    }


def _normalize_scalar(
    value: Any, config: ObservabilityConfig
) -> str | int | float | bool | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _redact(value, config)[: config.limits.max_string_length]
    return None


def _prohibited_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _PROHIBITED_KEY_PARTS)


def _limit(value: Any, config: ObservabilityConfig) -> str:
    try:
        return str(value)[: config.limits.max_string_length]
    except Exception:
        return "<unprintable>"


def _redact(value: str, config: ObservabilityConfig) -> str:
    redacted = value
    for sensitive in config.sensitive_values:
        if sensitive:
            redacted = redacted.replace(sensitive, "[REDACTED]")
    return redacted
