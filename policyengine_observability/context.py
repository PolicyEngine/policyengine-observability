from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import ObservabilityConfig


@dataclass
class ErrorRecord:
    type: str
    message: str
    handled: bool
    stack: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "handled": self.handled,
            "stack": self.stack,
        }


@dataclass
class OperationObservabilityContext:
    config: ObservabilityConfig
    name: str
    flavor: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    timings_ms: dict[str, float] = field(default_factory=dict)
    timing_counts: dict[str, int] = field(default_factory=dict)
    emit_log: bool = True
    record_metric: bool = True
    started_at: float = field(default_factory=time.perf_counter)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    error: ErrorRecord | None = None
    emitted: bool = False
    metric_recorded: bool = False
    span_handle: Any = None
    context_token: Any = None

    def set_attribute(self, key: str, value: Any) -> None:
        if value is None:
            return
        if hasattr(value, "value"):
            value = value.value
        self.attributes[key] = value

    def duration_seconds(self) -> float:
        return time.perf_counter() - self.started_at

    def metric_attributes(self, **extra: Any) -> dict[str, str]:
        attrs: dict[str, Any] = {
            "service.name": self.config.service_name,
            "service.role": self.config.service_role,
            "deployment.environment": self.config.environment,
            "operation": self.name,
            "flavor": self.flavor,
        }
        for key in self.config.metric_attribute_keys:
            if key in self.attributes:
                attrs[key] = self.attributes[key]
        attrs.update(extra)
        return _metric_attrs(attrs, self.config.metric_attribute_keys)

    def span_attributes(self, **extra: Any) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "service.name": self.config.service_name,
            "service.role": self.config.service_role,
            "deployment.environment": self.config.environment,
            "policyengine.operation": self.name,
            "policyengine.flavor": self.flavor,
        }
        attrs.update(
            {
                f"policyengine.{key}": value
                for key, value in self.attributes.items()
                if value is not None
            }
        )
        attrs.update(extra)
        return {
            key: value for key, value in attrs.items() if value is not None
        }

    def as_log_record(
        self,
        *,
        trace_id: str | None,
        span_id: str | None,
    ) -> dict[str, Any]:
        event = "operation_failed" if self.error else "operation_completed"
        return {
            "schema_version": "policyengine.observability.operation.v1",
            "event": event,
            "service_name": self.config.service_name,
            "service_role": self.config.service_role,
            "environment": self.config.environment,
            "created_at": self.created_at.isoformat(),
            "operation": self.name,
            "flavor": self.flavor,
            "trace_id": trace_id,
            "span_id": span_id,
            "duration_ms": round(self.duration_seconds() * 1000, 3),
            "timings_ms": dict(self.timings_ms),
            "timing_counts": dict(self.timing_counts),
            **self.attributes,
            "error": self.error.as_dict() if self.error else None,
        }


@dataclass
class RequestObservabilityContext:
    config: ObservabilityConfig
    request_id: str
    method: str
    route: str
    path: str
    endpoint: str | None
    query_keys: list[str]
    content_length_bytes: int | None
    inbound: dict[str, Any]
    internal_dispatch: bool = False
    started_at: float = field(default_factory=time.perf_counter)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    attributes: dict[str, Any] = field(default_factory=dict)
    timings_ms: dict[str, float] = field(default_factory=dict)
    timing_counts: dict[str, int] = field(default_factory=dict)
    status_code: int | None = None
    error: ErrorRecord | None = None
    emitted: bool = False
    request_metric_recorded: bool = False
    active_closed: bool = False
    span_closed: bool = False
    server_span_cm: Any = None
    server_span: Any = None
    context_token: Any = None
    operation_context: OperationObservabilityContext | None = None
    operation_token: Any = None

    def set_attribute(self, key: str, value: Any) -> None:
        if value is None:
            return
        if hasattr(value, "value"):
            value = value.value
        self.attributes[key] = value

    def duration_seconds(self) -> float:
        return time.perf_counter() - self.started_at

    def metric_attributes(self, **extra: Any) -> dict[str, str]:
        attrs: dict[str, Any] = {
            "service.name": self.config.service_name,
            "service.role": self.config.service_role,
            "deployment.environment": self.config.environment,
            "route": self.route,
            "method": self.method,
            "endpoint": self.endpoint,
        }
        if self.status_code is not None:
            attrs["status_code"] = str(self.status_code)
        for key in self.config.metric_attribute_keys:
            if key in self.attributes:
                attrs[key] = self.attributes[key]
        attrs.update(extra)
        return _metric_attrs(attrs, self.config.metric_attribute_keys)

    def span_attributes(self, **extra: Any) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "service.name": self.config.service_name,
            "service.role": self.config.service_role,
            "deployment.environment": self.config.environment,
            "http.request.method": self.method,
            "http.route": self.route,
            "url.path": self.path,
            "policyengine.endpoint": self.endpoint,
            "policyengine.request_id": self.request_id,
        }
        if self.status_code is not None:
            attrs["http.response.status_code"] = self.status_code
        for key in (
            "country_id",
            "backend",
            "requested_version",
            "resolved_channel",
            "auth_result",
        ):
            if key in self.attributes:
                attrs[f"policyengine.{key}"] = self.attributes[key]
        attrs.update(extra)
        return {
            key: value for key, value in attrs.items() if value is not None
        }

    def as_log_record(
        self,
        *,
        trace_id: str | None,
        span_id: str | None,
    ) -> dict[str, Any]:
        event = (
            "http_request_failed" if self.error else "http_request_completed"
        )
        status_code = self.status_code or (500 if self.error else None)
        return {
            "schema_version": "policyengine.observability.request.v1",
            "event": event,
            "service_name": self.config.service_name,
            "service_role": self.config.service_role,
            "environment": self.config.environment,
            "created_at": self.created_at.isoformat(),
            "request_id": self.request_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "method": self.method,
            "route": self.route,
            "path": self.path,
            "query_keys": self.query_keys,
            "endpoint": self.endpoint,
            "status_code": status_code,
            "duration_ms": round(self.duration_seconds() * 1000, 3),
            **self.inbound,
            "timings_ms": dict(self.timings_ms),
            "timing_counts": dict(self.timing_counts),
            **self.attributes,
            "error": self.error.as_dict() if self.error else None,
        }


def _metric_attrs(
    attrs: dict[str, Any],
    metric_attribute_keys: tuple[str, ...],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in metric_attribute_keys:
        value = attrs.get(key)
        if value is not None:
            result[key] = str(value)
    return result
