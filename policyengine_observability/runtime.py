"""Public runtime interface and component lifecycle."""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator, Iterator
from enum import Enum
from typing import Any

from . import _state
from ._metrics import MetricRecorder, _NoOpInstrument
from ._operations import OperationLifecycle
from ._requests import RequestLifecycle
from ._state import (
    OBSERVABILITY_INTERNAL_DISPATCH_HEADER as OBSERVABILITY_INTERNAL_DISPATCH_HEADER,
)
from ._state import (
    REQUEST_ID_HEADER as REQUEST_ID_HEADER,
)
from ._state import (
    TRACEPARENT_HEADER as TRACEPARENT_HEADER,
)
from ._state import (
    ContextState,
)
from ._tracing import TraceRecorder
from .config import ObservabilityConfig
from .context import OperationObservabilityContext, RequestObservabilityContext
from .destinations import LogDestinationManager
from .destinations.base import clamped
from .logging import (
    EVENT_LOGGER,
    INTERNAL_LOGGER,
    OPERATION_LOGGER,
    REQUEST_LOGGER,
    LogEmitter,
)
from .segments import SegmentRecorder


class ObservabilityRuntime:
    def __init__(
        self,
        config: ObservabilityConfig,
        *,
        segment_registry: type[Enum] | None = None,
    ) -> None:
        self.config = config
        self.segment_registry = segment_registry
        self.enabled = config.enabled
        self.trace = None
        self.propagate = None
        self.SpanKind = None
        self.Status = None
        self.StatusCode = None
        self.tracer_provider = None
        self.meter_provider = None
        self.tracer = None
        self.meter = None
        self.operation_duration = _NoOpInstrument()
        self.http_duration = _NoOpInstrument()
        self.segment_duration = _NoOpInstrument()
        self.calculate_duration = _NoOpInstrument()
        self.backend_duration = _NoOpInstrument()
        self.operations = _NoOpInstrument()
        self.requests = _NoOpInstrument()
        self.errors = _NoOpInstrument()
        self.rate_limited = _NoOpInstrument()
        self.failover_events = _NoOpInstrument()
        self.active_requests = _NoOpInstrument()
        self._httpx_instrumented = False
        self._emitting_internal_error = False
        self.log_destination_manager = LogDestinationManager(
            config=config,
            loggers={
                "request": REQUEST_LOGGER,
                "operation": OPERATION_LOGGER,
                "event": EVENT_LOGGER,
                "internal": INTERNAL_LOGGER,
            },
            serializer=self._json,
            on_failure=self._handle_destination_failure,
        )
        self._context_state = ContextState(self)
        self._operations = OperationLifecycle(self)
        self._requests = RequestLifecycle(self)
        self._segments = SegmentRecorder(self)
        self._logging = LogEmitter(self)
        self._metrics = MetricRecorder(self)
        self._tracing = TraceRecorder(self)

    @classmethod
    def disabled(cls) -> ObservabilityRuntime:
        return cls(ObservabilityConfig(enabled=False))

    def configure(self) -> None:
        self._configure_loggers()
        if not self.enabled:
            return
        self.log_destination_manager.configure()
        if not self.config.otel_enabled:
            return
        self._configure_otel()
        if self.config.instrument_httpx:
            self.instrument_httpx()

    def current_context(self) -> RequestObservabilityContext | None:
        return self._context_state.current_context()

    def current_operation(self) -> OperationObservabilityContext | None:
        return self._context_state.current_operation()

    def operation(self, name: str, *, flavor: str | None = None, **attrs: Any):
        return self._operations.operation(name, flavor=flavor, **attrs)

    def entrypoint(
        self,
        name: str | None = None,
        *,
        flavor: str | None = None,
        **attrs: Any,
    ):
        return self._operations.entrypoint(name, flavor=flavor, **attrs)

    def start_operation(
        self,
        name: str,
        *,
        flavor: str | None = None,
        parent_context: Any = None,
        timings: dict[str, float] | None = None,
        emit_log: bool = True,
        record_metric: bool = True,
        **attrs: Any,
    ) -> dict[str, Any]:
        return self._operations.start_operation(
            name,
            flavor=flavor,
            parent_context=parent_context,
            timings=timings,
            emit_log=emit_log,
            record_metric=record_metric,
            **attrs,
        )

    def end_operation(
        self, handle: dict[str, Any] | None, error: BaseException | None = None
    ) -> None:
        return self._operations.end_operation(handle, error)

    def complete_operation(
        self, operation: OperationObservabilityContext
    ) -> None:
        return self._operations.complete_operation(operation)

    def begin_request(
        self, context: RequestObservabilityContext, *, carrier: Any = None
    ) -> None:
        return self._requests.begin_request(context, carrier=carrier)

    def _begin_request_operation(self, *args: Any, **kwargs: Any) -> Any:
        return self._requests._begin_request_operation(*args, **kwargs)

    def finish_request(self, status_code: int) -> dict[str, str]:
        return self._requests.finish_request(status_code)

    def prepare_response(self, status_code: int) -> dict[str, str]:
        return self._requests.prepare_response(status_code)

    def complete_request(self, status_code: int | None = None) -> None:
        return self._requests.complete_request(status_code)

    def update_request_route(
        self, *, route: str | None = None, endpoint: str | None = None
    ) -> None:
        return self._requests.update_request_route(
            route=route, endpoint=endpoint
        )

    def teardown_request(self, exc: BaseException | None = None) -> None:
        return self._requests.teardown_request(exc)

    def set_attribute(self, key: str, value: Any) -> None:
        return self._requests.set_attribute(key, value)

    def segment(self, name: Any, **attrs: Any) -> Iterator[Any]:
        return self._segments.segment(name, **attrs)

    def _segment_context(self, *args: Any, **kwargs: Any) -> Any:
        return self._segments._segment_context(*args, **kwargs)

    def asegment(self, name: Any, **attrs: Any) -> AsyncIterator[Any]:
        return self._segments.asegment(name, **attrs)

    def collect_timings(self, name: str = "operation", **attrs: Any):
        return self._operations.collect_timings(name, **attrs)

    def start_scope(
        self,
        timings: dict[str, float],
        *,
        name: str = "operation",
        parent_context: Any = None,
        **attrs: Any,
    ) -> dict[str, Any]:
        return self._operations.start_scope(
            timings, name=name, parent_context=parent_context, **attrs
        )

    def annotate(
        self, handle: dict[str, Any] | None = None, **attrs: Any
    ) -> None:
        return self._operations.annotate(handle, **attrs)

    def end_scope(
        self, handle: dict[str, Any] | None, error: BaseException | None = None
    ) -> None:
        return self._operations.end_scope(handle, error)

    def mark(self, key: str, ms: float) -> None:
        return self._operations.mark(key, ms)

    def mark_ttft(self, key: str = "ttft_ms") -> None:
        return self._operations.mark_ttft(key)

    def mark_ttft_attribute(self, key: str = "ttft_ms") -> None:
        return self._operations.mark_ttft_attribute(key)

    def record_error(
        self,
        exc: BaseException,
        *,
        handled: bool,
        status_code: int | None = None,
        include_stack: bool = True,
    ) -> None:
        return self._logging.record_error(
            exc,
            handled=handled,
            status_code=status_code,
            include_stack=include_stack,
        )

    def record_event(self, event: str, **fields: Any) -> None:
        return self._logging.record_event(event, **fields)

    def traceparent_header(self) -> str | None:
        return self._tracing.traceparent_header()

    def capture_context(self):
        return self._tracing.capture_context()

    def emit_request_log(self, context: RequestObservabilityContext) -> None:
        return self._logging.emit_request_log(context)

    def emit_operation_log(
        self, operation: OperationObservabilityContext
    ) -> None:
        return self._logging.emit_operation_log(operation)

    def record_operation_metric(
        self, duration_seconds: float, attributes: dict[str, str]
    ) -> None:
        return self._metrics.record_operation_metric(
            duration_seconds, attributes
        )

    def record_request_metric(
        self, duration_seconds: float, attributes: dict[str, str]
    ) -> None:
        return self._metrics.record_request_metric(
            duration_seconds, attributes
        )

    def record_segment_metric(
        self,
        segment: str,
        duration_seconds: float,
        attributes: dict[str, str],
        *,
        backend_segment: bool = False,
    ) -> None:
        return self._metrics.record_segment_metric(
            segment,
            duration_seconds,
            attributes,
            backend_segment=backend_segment,
        )

    def record_error_metric(self, attributes: dict[str, str]) -> None:
        return self._metrics.record_error_metric(attributes)

    def record_rate_limited_metric(self, attributes: dict[str, str]) -> None:
        return self._metrics.record_rate_limited_metric(attributes)

    def record_failover_event_metric(self, attributes: dict[str, str]) -> None:
        return self._metrics.record_failover_event_metric(attributes)

    def record_active_request(
        self, delta: int, attributes: dict[str, str]
    ) -> None:
        return self._metrics.record_active_request(delta, attributes)

    def instrument_fastapi(self, app: Any) -> None:
        return self._tracing.instrument_fastapi(app)

    def instrument_httpx(self) -> None:
        return self._tracing.instrument_httpx()

    def shutdown(self) -> None:
        budget = clamped(
            self.config.shutdown_timeout_seconds,
            low=0.0,
            high=60.0,
            default=ObservabilityConfig.shutdown_timeout_seconds,
        )
        providers = [
            ("trace", self.tracer_provider),
            ("metrics", self.meter_provider),
        ]
        providers = [
            (name, provider)
            for name, provider in providers
            if provider is not None
        ]
        # Destination close is inherently deadline-bounded, so it runs
        # inline and first, with a deadline that leaves room for the
        # provider flush when there is one. Everything below fits inside
        # the one shutdown budget by construction.
        started = time.monotonic()
        try:
            self.log_destination_manager.close(
                budget / 2 if providers else budget
            )
        except BaseException as exc:
            self.log_observability_failure("logging.destination_close", exc)
        if not providers:
            return
        remaining = max(0.0, budget - (time.monotonic() - started))

        def flush() -> None:
            for name, provider in providers:
                try:
                    provider.shutdown()
                except BaseException as exc:
                    self.log_observability_failure(
                        f"otel.{name}_shutdown",
                        exc,
                    )

        thread = threading.Thread(
            target=flush,
            name="policyengine-otel-shutdown",
            daemon=True,
        )
        thread.start()
        thread.join(timeout=remaining)
        if thread.is_alive():
            self.log_observability_failure(
                "otel.shutdown_timeout",
                TimeoutError("OpenTelemetry shutdown timed out."),
                timeout_seconds=remaining,
            )

    def shutdown_tracing(self) -> None:
        self.shutdown()

    def restart_log_destinations(self) -> None:
        """Close and rebuild log destinations from configuration.

        Call ONLY from single-threaded lifecycle moments — a
        post-snapshot-restore hook, a post-fork hook, before serving
        traffic. There is deliberately no locking here: under that
        contract there is no concurrency, and a violated contract costs
        at most a counted drop into a closing destination.

        A no-op when observability is disabled, mirroring configure():
        the kill switch must hold across forks and snapshot restores.
        """
        if not self.enabled:
            return
        self.log_destination_manager.configure()

    def log_observability_failure(
        self, operation: str, exc: BaseException, **fields: Any
    ) -> None:
        return self._logging.log_observability_failure(
            operation, exc, **fields
        )

    def _configure_loggers(self, *args: Any, **kwargs: Any) -> Any:
        return self._logging._configure_loggers(*args, **kwargs)

    def _emit_structured_log(self, *args: Any, **kwargs: Any) -> Any:
        return self._logging._emit_structured_log(*args, **kwargs)

    def _handle_destination_failure(self, *args: Any, **kwargs: Any) -> Any:
        return self._logging._handle_destination_failure(*args, **kwargs)

    def _severity_for_log_record(self, *args: Any, **kwargs: Any) -> Any:
        return self._logging._severity_for_log_record(*args, **kwargs)

    def _int_or_none(self, *args: Any, **kwargs: Any) -> Any:
        return self._logging._int_or_none(*args, **kwargs)

    def _internal_error_payload(self, *args: Any, **kwargs: Any) -> Any:
        return self._logging._internal_error_payload(*args, **kwargs)

    def _configure_otel(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._configure_otel(*args, **kwargs)

    def _add_trace_exporter(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._add_trace_exporter(*args, **kwargs)

    def _metric_reader(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._metric_reader(*args, **kwargs)

    def _configure_instruments(self, *args: Any, **kwargs: Any) -> Any:
        return self._metrics._configure_instruments(*args, **kwargs)

    def _instrument(self, *args: Any, **kwargs: Any) -> Any:
        return self._metrics._instrument(*args, **kwargs)

    def _start_request_span(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._start_request_span(*args, **kwargs)

    def _close_request_span(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._close_request_span(*args, **kwargs)

    def _safe_span(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._safe_span(*args, **kwargs)

    def _start_span(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._start_span(*args, **kwargs)

    def _end_span(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._end_span(*args, **kwargs)

    def _start_segment_tree_node(self, *args: Any, **kwargs: Any) -> Any:
        return self._segments._start_segment_tree_node(*args, **kwargs)

    def _finish_segment_tree_node(self, *args: Any, **kwargs: Any) -> Any:
        return self._segments._finish_segment_tree_node(*args, **kwargs)

    def _reset_segment_tree_stack(self, *args: Any, **kwargs: Any) -> Any:
        return self._segments._reset_segment_tree_stack(*args, **kwargs)

    def _segment_tree_owner(self, *args: Any, **kwargs: Any) -> Any:
        return self._segments._segment_tree_owner(*args, **kwargs)

    def _safe_segment_tree_attrs(self, *args: Any, **kwargs: Any) -> Any:
        return self._segments._safe_segment_tree_attrs(*args, **kwargs)

    def _record_segment_flat_timing(self, *args: Any, **kwargs: Any) -> Any:
        return self._segments._record_segment_flat_timing(*args, **kwargs)

    def _record_segment_safely(self, *args: Any, **kwargs: Any) -> Any:
        return self._segments._record_segment_safely(*args, **kwargs)

    def _record_timing(self, *args: Any, **kwargs: Any) -> Any:
        return self._segments._record_timing(*args, **kwargs)

    def _segment_span_attributes(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._segment_span_attributes(*args, **kwargs)

    def _span_name(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._span_name(*args, **kwargs)

    def _start_implicit_operation(self, *args: Any, **kwargs: Any) -> Any:
        return self._operations._start_implicit_operation(*args, **kwargs)

    def _coerce_segment(self, *args: Any, **kwargs: Any) -> Any:
        return self._segments._coerce_segment(*args, **kwargs)

    def _set_current_span_attributes(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._set_current_span_attributes(*args, **kwargs)

    def _current_span(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._current_span(*args, **kwargs)

    def _trace_ids(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._trace_ids(*args, **kwargs)

    def _extract_context(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._extract_context(*args, **kwargs)

    def _record_exception_on_span(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._record_exception_on_span(*args, **kwargs)

    def _add_span_event(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracing._add_span_event(*args, **kwargs)

    def _close_active_request(self, *args: Any, **kwargs: Any) -> Any:
        return self._requests._close_active_request(*args, **kwargs)

    def _reset_request_operation_context(
        self, *args: Any, **kwargs: Any
    ) -> Any:
        return self._requests._reset_request_operation_context(*args, **kwargs)

    def _reset_request_context(self, *args: Any, **kwargs: Any) -> Any:
        return self._requests._reset_request_context(*args, **kwargs)

    def _safe_perf_counter(self, *args: Any, **kwargs: Any) -> Any:
        return self._segments._safe_perf_counter(*args, **kwargs)

    def _safe_str(self, *args: Any, **kwargs: Any) -> Any:
        return self._logging._safe_str(*args, **kwargs)

    def _safe_traceback(self, *args: Any, **kwargs: Any) -> Any:
        return self._logging._safe_traceback(*args, **kwargs)

    def _json(self, *args: Any, **kwargs: Any) -> Any:
        return self._logging._json(*args, **kwargs)

    def _write_stderr(self, *args: Any, **kwargs: Any) -> Any:
        return self._logging._write_stderr(*args, **kwargs)


_RUNTIME = ObservabilityRuntime(ObservabilityConfig())


def set_observability_runtime(runtime: ObservabilityRuntime) -> None:
    global _RUNTIME
    _RUNTIME = runtime
    for context_var in (
        _state._REQUEST_CONTEXT,
        _state._OPERATION_CONTEXT,
        _state._TIMINGS,
        _state._TURN_START,
    ):
        try:
            context_var.set(None)
        except BaseException:
            continue


def observability_runtime() -> ObservabilityRuntime:
    return _RUNTIME
