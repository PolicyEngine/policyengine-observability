from __future__ import annotations

import inspect
import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from .config import ObservabilityConfig
from .delivery import DeliveryManager
from .destinations import StdoutLogDestination
from .diagnostics import Diagnostics
from .otel import OTelRuntime, SpanHandle, captured_at_is_recent
from .schema import build_record, normalize_attributes

REQUEST_ID_HEADER = "X-PolicyEngine-Request-Id"
TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(slots=True)
class _RequestState:
    request_id: str
    method: str
    route: str
    start_time: float
    span: SpanHandle | None
    token: Token[Any] | None = None
    status_code: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error: BaseException | None = None
    completed: bool = False


@dataclass(slots=True)
class _OperationState:
    name: str
    kind: str
    request_id: str | None
    start_time: float
    span: SpanHandle | None
    token: Token[Any] | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error: BaseException | None = None
    completed: bool = False


@dataclass(slots=True)
class _ChildSpanState:
    name: str
    start_time: float
    span: SpanHandle | None


class ObservabilityRuntime:
    def __init__(self, config: ObservabilityConfig) -> None:
        self.config = config
        self.diagnostics = Diagnostics()
        self._request_state: ContextVar[_RequestState | None] = ContextVar(
            f"policyengine_request_{id(self)}", default=None
        )
        self._operation_state: ContextVar[_OperationState | None] = ContextVar(
            f"policyengine_operation_{id(self)}", default=None
        )
        self._shutdown_lock = threading.Lock()
        self._closed = False
        self._delivery = self._new_delivery()
        self._otel = self._new_otel()
        self.diagnostics.add_listener(self._record_diagnostic_metric)
        self._logging_handlers: list[
            tuple[logging.Logger, ObservabilityLogHandler]
        ] = []
        for warning in config.diagnostics():
            self.diagnostics.report("configuration", warning)
        if config.logging.capture_standard_library:
            instrument_logging(
                logging.getLogger(),
                self,
                replace=config.logging.replace_existing_handlers,
            )

    def _new_delivery(self) -> DeliveryManager:
        try:
            return DeliveryManager(self.config, self.diagnostics)
        except Exception as exc:
            self.diagnostics.report("delivery.configure", exc)
            fallback = ObservabilityConfig(
                service=self.config.service,
                deployment=self.config.deployment,
                logging=self.config.logging.__class__(
                    destinations=(StdoutLogDestination(),),
                ),
                otel=self.config.otel,
                limits=self.config.limits,
                application_attribute_keys=(
                    self.config.application_attribute_keys
                ),
                dispatch_attribute_keys=self.config.dispatch_attribute_keys,
                metric_attribute_keys=self.config.metric_attribute_keys,
                sensitive_values=self.config.sensitive_values,
            )
            return DeliveryManager(fallback, self.diagnostics)

    def _new_otel(self) -> OTelRuntime:
        try:
            return OTelRuntime(
                self.config,
                self.diagnostics,
                queue_depth=lambda: self._delivery.queue_depth,
            )
        except Exception as exc:
            self.diagnostics.report("otel.runtime", exc)
            return OTelRuntime(
                ObservabilityConfig(
                    service=self.config.service,
                    deployment=self.config.deployment,
                    logging=self.config.logging,
                    otel=self.config.otel.__class__(enabled=False),
                    limits=self.config.limits,
                    application_attribute_keys=(
                        self.config.application_attribute_keys
                    ),
                    dispatch_attribute_keys=(
                        self.config.dispatch_attribute_keys
                    ),
                    metric_attribute_keys=self.config.metric_attribute_keys,
                    sensitive_values=self.config.sensitive_values,
                ),
                self.diagnostics,
                queue_depth=lambda: self._delivery.queue_depth,
            )

    def _record_diagnostic_metric(self, name: str, value: int) -> None:
        if "dropped" in name or name.endswith("queue_full"):
            self._otel.record_dropped(name, value)
        elif "export_failure" in name:
            self._otel.record_exporter_failure(name, value)

    def operation(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        remote_context: Mapping[str, Any] | None = None,
        independent_retry: bool = False,
        aggregate: bool = False,
    ) -> _ScopeManager:
        return _ScopeManager(
            self,
            scope_type="operation",
            name=name,
            attributes=attributes,
            remote_context=remote_context,
            independent_retry=independent_retry,
            aggregate=aggregate,
        )

    def span(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> _ScopeManager:
        return _ScopeManager(
            self,
            scope_type="span",
            name=name,
            attributes=attributes,
        )

    def _scope_copy(self, manager: _ScopeManager) -> _ScopeManager:
        return _ScopeManager(
            self,
            scope_type=manager.scope_type,
            name=manager.name,
            attributes=manager.attributes,
            remote_context=manager.remote_context,
            independent_retry=manager.independent_retry,
            aggregate=manager.aggregate,
        )

    def begin_request(
        self,
        *,
        headers: Mapping[str, str],
        method: str,
        route: str,
    ) -> str:
        request_id = _request_id(_header_value(headers, REQUEST_ID_HEADER))
        try:
            parent = self._otel.extract(headers)
            span = self._otel.start_span(
                f"{method.upper()} {route}",
                kind=_span_kind("SERVER"),
                parent_context=parent,
                attributes={
                    "http.request.method": method.upper(),
                    "http.route": route,
                },
            )
            state = _RequestState(
                request_id=request_id,
                method=method.upper(),
                route=route,
                start_time=time.perf_counter(),
                span=span,
            )
            state.token = self._request_state.set(state)
        except Exception as exc:
            self.diagnostics.report("request.begin", exc)
        return request_id

    def update_request_route(self, route: str) -> None:
        state = self._request_state.get()
        if state is None or state.completed:
            return
        state.route = route
        self._otel.set_span_attributes({"http.route": route})

    def response_headers(self) -> dict[str, str]:
        state = self._request_state.get()
        if state is None:
            return {}
        headers = {REQUEST_ID_HEADER: state.request_id}
        carrier: dict[str, str] = {}
        self._otel.inject(carrier)
        if TRACEPARENT_HEADER in carrier:
            headers[TRACEPARENT_HEADER] = carrier[TRACEPARENT_HEADER]
        return headers

    def end_request(
        self,
        *,
        status_code: int | None,
        error: BaseException | None = None,
    ) -> None:
        state = self._request_state.get()
        if state is None or state.completed:
            return
        state.completed = True
        state.status_code = status_code
        state.error = error
        duration = max(0.0, time.perf_counter() - state.start_time)
        outcome = _outcome(status_code, error)
        metric_values = self._metric_base()
        metric_values.update(
            {
                "http.route": state.route,
                "http.request.method": state.method,
                "http.response.status_code_class": _status_class(status_code),
                "operation.kind": "request",
                "outcome": outcome,
            }
        )
        self._otel.set_span_attributes(
            {
                "http.route": state.route,
                "http.response.status_code": status_code or 0,
                "policyengine.request.id": state.request_id,
                "policyengine.outcome": outcome,
            }
        )
        context = {
            "request.id": state.request_id,
            "http.request.method": state.method,
            "http.route": state.route,
            "http.response.status_code": status_code,
            "duration_ms": round(duration * 1_000, 3),
            "outcome": outcome,
            **self._otel.current_correlation(),
        }
        self._emit_record(
            severity="ERROR" if outcome == "error" else "INFO",
            event_name="request.completed",
            context=context,
            attributes=state.attributes,
            error=error,
        )
        self._otel.record_request(duration, metric_values)
        if error is not None:
            self._otel.record_error(metric_values)
        self._otel.end_span(state.span, error)
        self._reset_request(state)

    def set_context(self, **attributes: Any) -> None:
        safe, omitted = normalize_attributes(
            attributes,
            self.config,
            allowed_keys=(
                self.config.application_attribute_keys
                | self.config.dispatch_attribute_keys
            ),
        )
        request = self._request_state.get()
        operation = self._operation_state.get()
        target = operation or request
        if target is not None:
            target.attributes.update(safe)
        if safe:
            self._otel.set_span_attributes(safe)
        if omitted:
            self.diagnostics.increment("attributes.omitted", omitted)

    def event(
        self,
        name: str,
        *,
        severity: str = "INFO",
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self._emit_record(
            severity=severity,
            event_name=name,
            context=self._active_context_fields(),
            attributes=attributes,
        )

    def log(
        self,
        message: str,
        *,
        severity: str = "INFO",
        attributes: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._emit_record(
            severity=severity,
            message=message,
            context=self._active_context_fields(),
            attributes=attributes,
            error=error,
        )

    def record_exception(
        self,
        error: Exception,
        *,
        handled: bool,
        status_code: int | None = None,
    ) -> None:
        request = self._request_state.get()
        operation = self._operation_state.get()
        if request is not None:
            request.error = error
            if status_code is not None:
                request.status_code = status_code
        if operation is not None:
            operation.error = error
        context = {
            **self._active_context_fields(),
            "error.handled": handled,
        }
        if status_code is not None:
            context["http.response.status_code"] = status_code
        self._emit_record(
            severity="ERROR",
            event_name="exception.recorded",
            context=context,
            error=error,
        )
        self._otel.record_error(
            {
                **self._metric_base(),
                "operation.kind": "handled" if handled else "unhandled",
                "outcome": "error",
            }
        )

    def capture_context(self) -> dict[str, str]:
        carrier: dict[str, str] = {}
        self._otel.inject(carrier)
        captured: dict[str, str] = {
            key: value
            for key, value in carrier.items()
            if key.lower() in {TRACEPARENT_HEADER, TRACESTATE_HEADER}
        }
        captured["captured_at"] = (
            datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )
        request = self._request_state.get()
        operation = self._operation_state.get()
        if request is not None:
            captured["request_id"] = request.request_id
        elif operation is not None and operation.request_id:
            captured["request_id"] = operation.request_id
        source = operation.attributes if operation is not None else {}
        if request is not None:
            source = {**request.attributes, **source}
        for key in self.config.dispatch_attribute_keys:
            value = source.get(key)
            if isinstance(value, (str, int)):
                captured[key] = str(value)[
                    : self.config.limits.max_string_length
                ]
        return captured

    def inject_http_headers(self, headers: dict[str, str]) -> None:
        self._otel.inject(headers)
        if REQUEST_ID_HEADER not in headers:
            request = self._request_state.get()
            operation = self._operation_state.get()
            request_id = (
                request.request_id
                if request is not None
                else operation.request_id
                if operation is not None
                else None
            )
            if request_id:
                headers[REQUEST_ID_HEADER] = request_id

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._closed:
                return
            self._closed = True
            logging_timeout = _bounded_shutdown_timeout(
                self.config.logging.shutdown_timeout_seconds,
                default=2.0,
            )
            otel_timeout = _bounded_shutdown_timeout(
                self.config.otel.shutdown_timeout_seconds,
                default=3.0,
            )
            try:
                try:
                    self._delivery.close(logging_timeout)
                except Exception as exc:
                    self.diagnostics.report("logging.shutdown", exc)
                try:
                    _run_bounded(
                        lambda: self._otel.shutdown(otel_timeout),
                        otel_timeout,
                        self.diagnostics,
                        "otel.shutdown_deadline",
                    )
                except Exception as exc:
                    self.diagnostics.report("otel.shutdown", exc)
            finally:
                self._remove_logging_handlers()

    def restart_after_snapshot(self) -> None:
        """Rebuild process-local state after a fork or snapshot restore.

        Call this only in a single-threaded lifecycle callback before the
        copied process accepts application work. Inherited workers and locks
        are abandoned because their owning threads may not exist in the new
        process.
        """

        self._shutdown_lock = threading.Lock()
        self.diagnostics.restart_after_process_duplication()
        self._request_state = ContextVar(
            f"policyengine_request_{id(self)}", default=None
        )
        self._operation_state = ContextVar(
            f"policyengine_operation_{id(self)}", default=None
        )
        for _logger, handler in self._logging_handlers:
            try:
                handler.createLock()
            except Exception as exc:
                self.diagnostics.report("process.logging_handler_lock", exc)
        self._delivery = self._new_delivery()
        self._otel = self._new_otel()
        self._closed = False

    def _start_operation(
        self,
        name: str,
        attributes: Mapping[str, Any] | None,
        remote_context: Mapping[str, Any] | None,
        independent_retry: bool,
        aggregate: bool,
    ) -> _OperationState:
        safe, omitted = normalize_attributes(
            attributes,
            self.config,
            allowed_keys=(
                self.config.application_attribute_keys
                | self.config.dispatch_attribute_keys
            ),
        )
        if omitted:
            self.diagnostics.increment("attributes.omitted", omitted)
        parent = None
        links: list[Any] = []
        request_id: str | None = None
        if remote_context:
            request_id = _valid_request_id(remote_context.get("request_id"))
            carrier = {
                key: str(value)
                for key, value in remote_context.items()
                if key.lower() in {TRACEPARENT_HEADER, TRACESTATE_HEADER}
            }
            extracted = self._otel.extract(carrier)
            direct = (
                not independent_retry
                and not aggregate
                and captured_at_is_recent(
                    remote_context.get("captured_at"),
                    self.config.limits.async_parent_max_age_seconds,
                )
            )
            if direct:
                parent = extracted
            else:
                link = self._otel.link(self._otel.remote_span_context(carrier))
                if link is not None:
                    links.append(link)
                parent = self._otel.empty_context()
        active_request = self._request_state.get()
        if request_id is None and active_request is not None:
            request_id = active_request.request_id
        span = self._otel.start_span(
            name,
            kind=_span_kind("CONSUMER") if remote_context else None,
            attributes={
                "operation.name": name,
                "operation.kind": "operation",
                **safe,
            },
            parent_context=parent,
            links=links,
        )
        state = _OperationState(
            name=name,
            kind="operation",
            request_id=request_id,
            start_time=time.perf_counter(),
            span=span,
            attributes=safe,
        )
        state.token = self._operation_state.set(state)
        return state

    def _finish_operation(
        self,
        state: _OperationState | None,
        error: BaseException | None,
    ) -> None:
        if state is None or state.completed:
            return
        state.completed = True
        state.error = error or state.error
        duration = max(0.0, time.perf_counter() - state.start_time)
        outcome = "error" if state.error is not None else "success"
        context = {
            "operation.name": state.name,
            "operation.kind": state.kind,
            "request.id": state.request_id,
            "duration_ms": round(duration * 1_000, 3),
            "outcome": outcome,
            **self._otel.current_correlation(),
        }
        self._emit_record(
            severity="ERROR" if state.error is not None else "INFO",
            event_name="operation.completed",
            context=context,
            attributes=state.attributes,
            error=state.error,
        )
        metrics = {
            **self._metric_base(),
            "operation.name": state.name,
            "operation.kind": state.kind,
            "outcome": outcome,
        }
        self._otel.record_operation(duration, metrics)
        if state.error is not None:
            self._otel.record_error(metrics)
        self._otel.end_span(state.span, state.error)
        if state.token is not None:
            try:
                self._operation_state.reset(state.token)
            except Exception as exc:
                self.diagnostics.report("operation.context_reset", exc)

    def _start_child_span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None,
    ) -> _ChildSpanState:
        safe, omitted = normalize_attributes(
            attributes,
            self.config,
            allowed_keys=(
                self.config.application_attribute_keys
                | self.config.dispatch_attribute_keys
            ),
        )
        if omitted:
            self.diagnostics.increment("attributes.omitted", omitted)
        return _ChildSpanState(
            name=name,
            start_time=time.perf_counter(),
            span=self._otel.start_span(name, attributes=safe),
        )

    def _finish_child_span(
        self,
        state: _ChildSpanState | None,
        error: BaseException | None,
    ) -> None:
        if state is None:
            return
        self._otel.end_span(state.span, error)

    def _active_context_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        request = self._request_state.get()
        operation = self._operation_state.get()
        if request is not None:
            fields.update(
                {
                    "request.id": request.request_id,
                    "http.request.method": request.method,
                    "http.route": request.route,
                }
            )
        if operation is not None:
            fields.update(
                {
                    "operation.name": operation.name,
                    "operation.kind": operation.kind,
                }
            )
            if operation.request_id and "request.id" not in fields:
                fields["request.id"] = operation.request_id
        fields.update(self._otel.current_correlation())
        return fields

    def _metric_base(self) -> dict[str, Any]:
        return {
            "service.name": self.config.service.name,
            "service.role": self.config.service.role,
            "deployment.environment.name": (
                self.config.deployment.environment
            ),
            "cloud.platform": self.config.deployment.platform,
        }

    def _emit_record(self, **kwargs: Any) -> None:
        try:
            self._delivery.emit(build_record(self.config, **kwargs))
        except Exception as exc:
            self.diagnostics.report("record.emit", exc)

    def _reset_request(self, state: _RequestState) -> None:
        if state.token is None:
            return
        try:
            self._request_state.reset(state.token)
        except Exception as exc:
            self.diagnostics.report("request.context_reset", exc)

    def _register_logging_handler(
        self,
        logger: logging.Logger,
        handler: ObservabilityLogHandler,
    ) -> None:
        self._logging_handlers.append((logger, handler))

    def _remove_logging_handlers(self) -> None:
        for logger, handler in self._logging_handlers:
            try:
                logger.removeHandler(handler)
            except Exception as exc:
                self.diagnostics.report("logging.handler_remove", exc)
        self._logging_handlers.clear()


class _ScopeManager:
    def __init__(
        self,
        runtime: ObservabilityRuntime,
        *,
        scope_type: str,
        name: str,
        attributes: Mapping[str, Any] | None,
        remote_context: Mapping[str, Any] | None = None,
        independent_retry: bool = False,
        aggregate: bool = False,
    ) -> None:
        self.runtime = runtime
        self.scope_type = scope_type
        self.name = str(name)
        self.attributes = attributes
        self.remote_context = remote_context
        self.independent_retry = independent_retry
        self.aggregate = aggregate
        self.state: _OperationState | _ChildSpanState | None = None

    def __enter__(self) -> Any:
        try:
            if self.scope_type == "operation":
                self.state = self.runtime._start_operation(
                    self.name,
                    self.attributes,
                    self.remote_context,
                    self.independent_retry,
                    self.aggregate,
                )
            else:
                self.state = self.runtime._start_child_span(
                    self.name, self.attributes
                )
        except Exception as exc:
            self.runtime.diagnostics.report(
                f"{self.scope_type}.start", exc, name=self.name
            )
            self.state = None
        return self.state

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            if self.scope_type == "operation":
                self.runtime._finish_operation(self.state, exc)  # type: ignore[arg-type]
            else:
                self.runtime._finish_child_span(self.state, exc)  # type: ignore[arg-type]
        except Exception as observability_error:
            self.runtime.diagnostics.report(
                f"{self.scope_type}.finish",
                observability_error,
                name=self.name,
            )
        return False

    async def __aenter__(self) -> Any:
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return self.__exit__(exc_type, exc, traceback)

    def __call__(self, function: Any) -> Any:
        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async with self.runtime._scope_copy(self):
                    return await function(*args, **kwargs)

            return async_wrapper

        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with self.runtime._scope_copy(self):
                return function(*args, **kwargs)

        return wrapper


class ObservabilityLogHandler(logging.Handler):
    _IGNORED_PREFIXES = (
        "google.",
        "grpc",
        "opentelemetry.",
        "policyengine_observability.",
    )

    def __init__(self, runtime: ObservabilityRuntime) -> None:
        super().__init__(level=runtime.config.logging.minimum_severity)
        self.runtime = runtime

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(self._IGNORED_PREFIXES):
            return
        try:
            attributes = getattr(record, "policyengine_attributes", None)
            error = record.exc_info[1] if record.exc_info else None
            self.runtime.log(
                record.getMessage(),
                severity=record.levelname,
                attributes=attributes
                if isinstance(attributes, Mapping)
                else None,
                error=error,
            )
        except Exception as exc:
            self.runtime.diagnostics.report("logging.handler_emit", exc)


def instrument_logging(
    logger: logging.Logger,
    runtime: ObservabilityRuntime,
    *,
    replace: bool = False,
) -> ObservabilityLogHandler:
    for handler in logger.handlers:
        if (
            isinstance(handler, ObservabilityLogHandler)
            and handler.runtime is runtime
        ):
            return handler
    if replace:
        logger.handlers.clear()
    handler = ObservabilityLogHandler(runtime)
    logger.addHandler(handler)
    runtime._register_logging_handler(logger, handler)
    return handler


def configure(config: ObservabilityConfig) -> ObservabilityRuntime:
    return ObservabilityRuntime(config)


def _request_id(value: Any) -> str:
    return _valid_request_id(value) or str(uuid.uuid4())


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _valid_request_id(value: Any) -> str | None:
    if isinstance(value, str) and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return None


def _status_class(status_code: int | None) -> str:
    if status_code is None or status_code < 100 or status_code > 599:
        return "unknown"
    return f"{status_code // 100}xx"


def _outcome(status_code: int | None, error: BaseException | None) -> str:
    if error is not None or (status_code is not None and status_code >= 500):
        return "error"
    if status_code is not None and status_code >= 400:
        return "client_error"
    return "success"


def _span_kind(name: str) -> Any:
    try:
        from opentelemetry.trace import SpanKind

        return getattr(SpanKind, name)
    except Exception:
        return None


def _run_bounded(
    function: Any,
    timeout_seconds: float,
    diagnostics: Diagnostics,
    diagnostic_name: str,
) -> None:
    completed = threading.Event()

    def run() -> None:
        try:
            function()
        except Exception as exc:
            diagnostics.report(diagnostic_name, exc)
        finally:
            completed.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    completed.wait(max(0.0, timeout_seconds))
    if not completed.is_set():
        diagnostics.increment("shutdown.timeout")
        diagnostics.report(
            diagnostic_name,
            "Operation exceeded the configured shutdown deadline.",
        )


def _bounded_shutdown_timeout(value: float, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return min(max(parsed, 0.0), 60.0)
