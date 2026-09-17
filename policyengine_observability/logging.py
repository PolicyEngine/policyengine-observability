"""Structured log emission and observability-failure handling."""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .context import (
    ErrorRecord,
    OperationObservabilityContext,
    RequestObservabilityContext,
    _metric_attrs,
)

if TYPE_CHECKING:
    from .runtime import ObservabilityRuntime


class PlainMessageFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def configure_plain_logger(logger: logging.Logger, level: int) -> None:
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(PlainMessageFormatter())
        logger.addHandler(handler)


REQUEST_LOGGER_NAME = "policyengine_observability.requests"
OPERATION_LOGGER_NAME = "policyengine_observability.operations"
EVENT_LOGGER_NAME = "policyengine_observability.events"
INTERNAL_LOGGER_NAME = "policyengine_observability.internal"

REQUEST_LOGGER = logging.getLogger(REQUEST_LOGGER_NAME)
OPERATION_LOGGER = logging.getLogger(OPERATION_LOGGER_NAME)
EVENT_LOGGER = logging.getLogger(EVENT_LOGGER_NAME)
INTERNAL_LOGGER = logging.getLogger(INTERNAL_LOGGER_NAME)


class LogEmitter:
    def __init__(self, runtime: ObservabilityRuntime) -> None:
        self.runtime = runtime

    def record_error(
        self,
        exc: BaseException,
        *,
        handled: bool,
        status_code: int | None = None,
        include_stack: bool = True,
    ) -> None:
        if not self.runtime.enabled:
            return
        try:
            context = self.runtime.current_context()
            operation = self.runtime.current_operation()
            error_record = ErrorRecord(
                type=type(exc).__name__,
                message=self.runtime._safe_str(exc),
                handled=handled,
                stack=(
                    self.runtime._safe_traceback(exc)
                    if include_stack
                    else None
                ),
            )
            if context is not None:
                if status_code is not None:
                    context.status_code = status_code
                context.error = error_record
                self.runtime.record_error_metric(
                    context.metric_attributes(error_type=type(exc).__name__)
                )
            elif operation is not None:
                operation.error = error_record
                self.runtime.record_error_metric(
                    operation.metric_attributes(error_type=type(exc).__name__)
                )
            else:
                return
            span = self.runtime._current_span()
            if span is not None:
                self.runtime._record_exception_on_span(
                    span,
                    exc,
                    handled=handled,
                    status_code=status_code,
                )
        except BaseException as observability_exc:
            self.runtime.log_observability_failure(
                "request.record_error",
                observability_exc,
                original_error_type=type(exc).__name__,
            )

    def record_event(self, event: str, **fields: Any) -> None:
        if not self.runtime.enabled:
            return
        try:
            context = self.runtime.current_context()
            operation = self.runtime.current_operation()
            base: dict[str, Any] = {
                "schema_version": "policyengine.observability.event.v1",
                "event": event,
                "service_name": self.runtime.config.service_name,
                "service_role": self.runtime.config.service_role,
                "environment": self.runtime.config.environment,
                "created_at": datetime.now(UTC).isoformat(),
            }
            if context is not None:
                trace_id, span_id = self.runtime._trace_ids()
                base.update(
                    {
                        "service_name": context.config.service_name,
                        "service_role": context.config.service_role,
                        "environment": context.config.environment,
                        "request_id": context.request_id,
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "route": context.route,
                        "path": context.path,
                    }
                )
            elif operation is not None:
                trace_id, span_id = self.runtime._trace_ids()
                base.update(
                    {
                        "service_name": operation.config.service_name,
                        "service_role": operation.config.service_role,
                        "environment": operation.config.environment,
                        "operation": operation.name,
                        "flavor": operation.flavor,
                        "trace_id": trace_id,
                        "span_id": span_id,
                    }
                )
            clean_fields = {
                key: value
                for key, value in fields.items()
                if value is not None
            }
            base.update(clean_fields)
            self.runtime._emit_structured_log(
                base,
                log_type="event",
                severity="INFO",
            )
            self.runtime._add_span_event(event, clean_fields)
            if event.startswith("modal_") or "fallback" in event:
                attrs = (
                    context.metric_attributes(event=event)
                    if context
                    else operation.metric_attributes(event=event)
                    if operation
                    else _metric_attrs(
                        {"event": event},
                        self.runtime.config.metric_attribute_keys,
                    )
                )
                self.runtime.record_failover_event_metric(attrs)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "request.record_event",
                exc,
                event_name=event,
            )

    def emit_request_log(self, context: RequestObservabilityContext) -> None:
        if not self.runtime.enabled:
            return
        try:
            if context.emitted:
                return
            context.emitted = True
            if (
                context.internal_dispatch
                or not context.config.request_logs_enabled
            ):
                return
            trace_id, span_id = self.runtime._trace_ids()
            payload = context.as_log_record(
                trace_id=trace_id,
                span_id=span_id,
            )
            self.runtime._emit_structured_log(
                payload,
                log_type="request",
                severity=self.runtime._severity_for_log_record(payload),
            )
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "request.emit_request_log",
                exc,
                request_id=getattr(context, "request_id", None),
            )

    def emit_operation_log(
        self,
        operation: OperationObservabilityContext,
    ) -> None:
        if not self.runtime.enabled:
            return
        try:
            if operation.emitted:
                return
            operation.emitted = True
            trace_id, span_id = self.runtime._trace_ids()
            payload = operation.as_log_record(
                trace_id=trace_id,
                span_id=span_id,
            )
            self.runtime._emit_structured_log(
                payload,
                log_type="operation",
                severity=self.runtime._severity_for_log_record(payload),
            )
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "operation.emit_log",
                exc,
                operation=getattr(operation, "name", None),
            )

    def log_observability_failure(
        self,
        operation: str,
        exc: BaseException,
        **fields: Any,
    ) -> None:
        payload = self.runtime._internal_error_payload(
            operation, exc, **fields
        )
        if self.runtime._emitting_internal_error:
            self.runtime._write_stderr(payload)
            return
        self.runtime._emitting_internal_error = True
        try:
            self.runtime._emit_structured_log(
                payload,
                log_type="internal",
                severity="ERROR",
            )
        except BaseException:
            self.runtime._write_stderr(payload)
        finally:
            self.runtime._emitting_internal_error = False

    def _configure_loggers(self) -> None:
        for logger in (
            REQUEST_LOGGER,
            OPERATION_LOGGER,
            EVENT_LOGGER,
            INTERNAL_LOGGER,
        ):
            configure_plain_logger(logger, self.runtime.config.log_level)

    def _emit_structured_log(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> None:
        try:
            if not self.runtime.log_destination_manager.configured:
                self.runtime._configure_loggers()
            self.runtime.log_destination_manager.emit(
                payload,
                log_type=log_type,
                severity=severity,
            )
        except BaseException as exc:
            if self.runtime._emitting_internal_error:
                self.runtime._write_stderr(payload)
            else:
                self.runtime.log_observability_failure(
                    "logging.emit",
                    exc,
                    log_type=log_type,
                )

    def _handle_destination_failure(
        self,
        operation: str,
        exc: BaseException,
        **fields: Any,
    ) -> None:
        if self.runtime._emitting_internal_error:
            self.runtime._write_stderr(
                self.runtime._internal_error_payload(operation, exc, **fields)
            )
            return
        self.runtime.log_observability_failure(operation, exc, **fields)

    def _severity_for_log_record(self, payload: dict[str, Any]) -> str:
        status_code = self.runtime._int_or_none(payload.get("status_code"))
        error = payload.get("error")
        handled = error.get("handled") if isinstance(error, dict) else None
        if status_code is not None and status_code >= 500:
            return "ERROR"
        if error is not None and handled is False:
            return "ERROR"
        if error is not None or (
            status_code is not None and status_code >= 400
        ):
            return "WARNING"
        return "INFO"

    def _int_or_none(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _internal_error_payload(
        self,
        operation: str,
        exc: BaseException,
        **fields: Any,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": "policyengine.observability.internal_error.v1",
            "event": "observability_internal_error",
            "service_name": self.runtime.config.service_name,
            "service_role": self.runtime.config.service_role,
            "environment": self.runtime.config.environment,
            "created_at": datetime.now(UTC).isoformat(),
            "operation": operation,
            "error": {
                "type": type(exc).__name__,
                "message": self.runtime._safe_str(exc),
                "stack": self.runtime._safe_traceback(exc),
            },
        }
        payload.update(
            {key: value for key, value in fields.items() if value is not None}
        )
        return payload

    def _safe_str(self, value: Any) -> str:
        try:
            return str(value)
        except BaseException:
            return f"<unprintable {type(value).__name__}>"

    def _safe_traceback(self, exc: BaseException) -> str:
        try:
            return "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        except BaseException:
            return ""

    def _json(self, payload: dict[str, Any]) -> str:
        try:
            return json.dumps(payload, sort_keys=True, default=str)
        except BaseException:
            return json.dumps(
                {
                    "schema_version": "policyengine.observability.internal_error.v1",
                    "event": "observability_internal_error",
                    "created_at": datetime.now(UTC).isoformat(),
                    "operation": "observability.failure_json",
                },
                sort_keys=True,
            )

    def _write_stderr(self, payload: dict[str, Any]) -> None:
        try:
            sys.stderr.write(self.runtime._json(payload) + "\n")
        except BaseException:
            return
