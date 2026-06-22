from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import sys
import threading
import time
import traceback
from typing import Any, AsyncIterator, Iterator

from .config import ObservabilityConfig
from .context import ErrorRecord
from .context import METRIC_ATTRIBUTE_KEYS
from .context import RequestObservabilityContext
from .context import _metric_attrs
from .logging import configure_plain_logger
from .segments import coerce_segment_name


OBSERVABILITY_INTERNAL_DISPATCH_HEADER = "X-PolicyEngine-Internal-Dispatch"
REQUEST_ID_HEADER = "X-PolicyEngine-Request-Id"
TRACEPARENT_HEADER = "traceparent"

REQUEST_LOGGER_NAME = "policyengine_observability.requests"
EVENT_LOGGER_NAME = "policyengine_observability.events"
INTERNAL_LOGGER_NAME = "policyengine_observability.internal"

REQUEST_LOGGER = logging.getLogger(REQUEST_LOGGER_NAME)
EVENT_LOGGER = logging.getLogger(EVENT_LOGGER_NAME)
INTERNAL_LOGGER = logging.getLogger(INTERNAL_LOGGER_NAME)

_REQUEST_CONTEXT: ContextVar[RequestObservabilityContext | None] = ContextVar(
    "policyengine_request_observability_context",
    default=None,
)
_TIMINGS: ContextVar[dict[str, float] | None] = ContextVar(
    "policyengine_observability_timings",
    default=None,
)
_TURN_START: ContextVar[float | None] = ContextVar(
    "policyengine_observability_turn_start",
    default=None,
)


class _NoOpInstrument:
    def add(self, *_args, **_kwargs) -> None:
        return None

    def record(self, *_args, **_kwargs) -> None:
        return None


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
        self.http_duration = _NoOpInstrument()
        self.segment_duration = _NoOpInstrument()
        self.calculate_duration = _NoOpInstrument()
        self.backend_duration = _NoOpInstrument()
        self.requests = _NoOpInstrument()
        self.errors = _NoOpInstrument()
        self.rate_limited = _NoOpInstrument()
        self.failover_events = _NoOpInstrument()
        self.active_requests = _NoOpInstrument()

    @classmethod
    def disabled(cls) -> "ObservabilityRuntime":
        return cls(ObservabilityConfig(enabled=False))

    def configure(self) -> None:
        self._configure_loggers()
        if not self.enabled or not self.config.otel_enabled:
            return
        self._configure_otel()

    def current_context(self) -> RequestObservabilityContext | None:
        try:
            return _REQUEST_CONTEXT.get()
        except BaseException as exc:
            self.log_observability_failure("context.current", exc)
            return None

    def begin_request(
        self,
        context: RequestObservabilityContext,
        *,
        carrier: Any = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            context.context_token = _REQUEST_CONTEXT.set(context)
            context.set_attribute("endpoint", context.endpoint)
            self._start_request_span(context, carrier=carrier)
            self.record_active_request(1, context.metric_attributes())
        except BaseException as exc:
            self.log_observability_failure("request.begin", exc)

    def finish_request(self, status_code: int) -> dict[str, str]:
        if not self.enabled:
            return {}
        headers: dict[str, str] = {}
        try:
            context = self.current_context()
            if context is None:
                return headers
            context.status_code = status_code
            self._set_current_span_attributes(context.span_attributes())
            headers[REQUEST_ID_HEADER] = context.request_id
            traceparent = self.traceparent_header()
            if traceparent:
                headers[TRACEPARENT_HEADER] = traceparent
            if status_code == 429:
                context.set_attribute("rate_limited", True)
                self.record_rate_limited_metric(context.metric_attributes())
            self.record_request_metric(
                context.duration_seconds(),
                context.metric_attributes(),
            )
            self._close_active_request(context)
        except BaseException as exc:
            self.log_observability_failure("request.finish", exc)
        return headers

    def teardown_request(self, exc: BaseException | None = None) -> None:
        if not self.enabled:
            return
        context = self.current_context()
        if context is None:
            return
        try:
            if exc is not None:
                self.record_error(exc, handled=False, status_code=500)
            self._close_active_request(context)
            self.emit_request_log(context)
        except BaseException as observability_exc:
            self.log_observability_failure(
                "request.teardown",
                observability_exc,
            )
        finally:
            self._close_request_span(context, exc)
            self._reset_request_context(context)

    def set_attribute(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        try:
            context = self.current_context()
            if context is not None:
                context.set_attribute(key, value)
                self._set_current_span_attributes(
                    context.span_attributes(**{f"policyengine.{key}": value})
                )
        except BaseException as exc:
            self.log_observability_failure(
                "request.set_attribute",
                exc,
                attribute=key,
            )

    @contextmanager
    def segment(self, name: Any, **attrs: Any) -> Iterator[Any]:
        if not self.enabled:
            yield None
            return
        segment_name = self._coerce_segment(name)
        start = self._safe_perf_counter(f"segment.{segment_name}.start")
        span_attrs = self._segment_span_attributes(attrs)
        span_name = self._span_name(segment_name)
        with self._safe_span(span_name, span_attrs) as span:
            try:
                yield span
            except BaseException:
                self._record_segment_safely(segment_name, start, attrs)
                raise
            else:
                self._record_segment_safely(segment_name, start, attrs)

    @asynccontextmanager
    async def asegment(self, name: Any, **attrs: Any) -> AsyncIterator[Any]:
        if not self.enabled:
            yield None
            return
        segment_name = self._coerce_segment(name)
        start = self._safe_perf_counter(f"segment.{segment_name}.start")
        span_attrs = self._segment_span_attributes(attrs)
        span_name = self._span_name(segment_name)
        with self._safe_span(span_name, span_attrs) as span:
            try:
                yield span
            except BaseException:
                self._record_segment_safely(segment_name, start, attrs)
                raise
            else:
                self._record_segment_safely(segment_name, start, attrs)

    @contextmanager
    def collect_timings(self, name: str = "operation", **attrs: Any):
        timings: dict[str, float] = {}
        handle = self.start_scope(timings, name=name, **attrs)
        error: BaseException | None = None
        try:
            yield timings
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.end_scope(handle, error)

    def start_scope(
        self,
        timings: dict[str, float],
        *,
        name: str = "operation",
        parent_context: Any = None,
        **attrs: Any,
    ) -> dict[str, Any]:
        handle = {
            "timings_token": None,
            "start_token": None,
            "context_token": None,
            "span": None,
        }
        try:
            handle["timings_token"] = _TIMINGS.set(timings)
        except BaseException as exc:
            self.log_observability_failure("scope.timings_set", exc)
        try:
            handle["start_token"] = _TURN_START.set(time.perf_counter())
        except BaseException as exc:
            self.log_observability_failure("scope.start_set", exc)
        if parent_context is not None and self.tracer is not None:
            try:
                from opentelemetry import context as otel_context

                handle["context_token"] = otel_context.attach(parent_context)
            except BaseException as exc:
                self.log_observability_failure("scope.context_attach", exc)
        try:
            if self.tracer is not None:
                handle["span"] = self._start_span(name, attrs)
        except BaseException as exc:
            self.log_observability_failure("scope.span_start", exc, span=name)
            handle["span"] = None
        return handle

    def annotate(self, handle: dict[str, Any] | None, **attrs: Any) -> None:
        if not handle:
            return
        span_handle = handle.get("span")
        if span_handle is None:
            return
        try:
            _cm, span = span_handle
            for key, value in attrs.items():
                if value is not None:
                    span.set_attribute(key, value)
        except BaseException as exc:
            self.log_observability_failure("scope.annotate", exc)

    def end_scope(
        self,
        handle: dict[str, Any] | None,
        error: BaseException | None = None,
    ) -> None:
        if not handle:
            return
        try:
            self._end_span(handle.get("span"), error)
        except BaseException as exc:
            self.log_observability_failure("scope.span_end", exc)
        context_token = handle.get("context_token")
        if context_token is not None:
            try:
                from opentelemetry import context as otel_context

                otel_context.detach(context_token)
            except BaseException as exc:
                self.log_observability_failure("scope.context_detach", exc)
        for var, key in (
            (_TIMINGS, "timings_token"),
            (_TURN_START, "start_token"),
        ):
            token = handle.get(key)
            if token is not None:
                try:
                    var.reset(token)
                except BaseException as exc:
                    self.log_observability_failure(
                        "scope.context_reset",
                        exc,
                        token=key,
                    )

    def mark(self, key: str, ms: float) -> None:
        try:
            timings = _TIMINGS.get()
            if timings is not None:
                timings[key] = round(float(ms), 1)
        except BaseException as exc:
            self.log_observability_failure("scope.mark", exc, key=key)

    def mark_ttft(self, key: str = "ttft_ms") -> None:
        try:
            start = _TURN_START.get()
            if start is not None:
                self.mark(key, (time.perf_counter() - start) * 1000.0)
        except BaseException as exc:
            self.log_observability_failure("scope.mark_ttft", exc)

    def record_error(
        self,
        exc: BaseException,
        *,
        handled: bool,
        status_code: int | None = None,
        include_stack: bool = True,
    ) -> None:
        if not self.enabled:
            return
        try:
            context = self.current_context()
            if context is None:
                return
            if status_code is not None:
                context.status_code = status_code
            context.error = ErrorRecord(
                type=type(exc).__name__,
                message=self._safe_str(exc),
                handled=handled,
                stack=(self._safe_traceback(exc) if include_stack else None),
            )
            self.record_error_metric(
                context.metric_attributes(error_type=type(exc).__name__)
            )
            span = self._current_span()
            if span is not None:
                self._record_exception_on_span(
                    span,
                    exc,
                    handled=handled,
                    status_code=status_code,
                )
        except BaseException as observability_exc:
            self.log_observability_failure(
                "request.record_error",
                observability_exc,
                original_error_type=type(exc).__name__,
            )

    def record_event(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        try:
            context = self.current_context()
            base: dict[str, Any] = {
                "schema_version": "policyengine.observability.event.v1",
                "event": event,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if context is not None:
                trace_id, span_id = self._trace_ids()
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
            clean_fields = {
                key: value
                for key, value in fields.items()
                if value is not None
            }
            base.update(clean_fields)
            EVENT_LOGGER.info(self._json(base))
            self._add_span_event(event, clean_fields)
            if event.startswith("modal_") or "fallback" in event:
                attrs = (
                    context.metric_attributes(event=event)
                    if context
                    else _metric_attrs({"event": event})
                )
                self.record_failover_event_metric(attrs)
        except BaseException as exc:
            self.log_observability_failure(
                "request.record_event",
                exc,
                event_name=event,
            )

    def traceparent_header(self) -> str | None:
        if not self.enabled or self.propagate is None:
            return None
        try:
            carrier: dict[str, str] = {}
            self.propagate.inject(carrier)
            return carrier.get(TRACEPARENT_HEADER)
        except BaseException as exc:
            self.log_observability_failure("request.traceparent_header", exc)
            return None

    def capture_context(self):
        if self.tracer is None:
            return None
        try:
            from opentelemetry import context as otel_context

            return otel_context.get_current()
        except BaseException as exc:
            self.log_observability_failure("otel.capture_context", exc)
            return None

    def emit_request_log(self, context: RequestObservabilityContext) -> None:
        if not self.enabled:
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
            trace_id, span_id = self._trace_ids()
            REQUEST_LOGGER.info(
                self._json(
                    context.as_log_record(
                        trace_id=trace_id,
                        span_id=span_id,
                    )
                )
            )
        except BaseException as exc:
            self.log_observability_failure(
                "request.emit_request_log",
                exc,
                request_id=getattr(context, "request_id", None),
            )

    def record_request_metric(
        self,
        duration_seconds: float,
        attributes: dict[str, str],
    ) -> None:
        try:
            self.http_duration.record(duration_seconds, attributes)
            self.requests.add(1, attributes)
        except BaseException as exc:
            self.log_observability_failure("metrics.record_request", exc)

    def record_segment_metric(
        self,
        segment: str,
        duration_seconds: float,
        attributes: dict[str, str],
        *,
        backend_segment: bool = False,
    ) -> None:
        try:
            segment_attributes = {**attributes, "segment": segment}
            self.segment_duration.record(duration_seconds, segment_attributes)
            if segment == "calculation":
                self.calculate_duration.record(duration_seconds, attributes)
            if backend_segment:
                self.backend_duration.record(
                    duration_seconds,
                    segment_attributes,
                )
        except BaseException as exc:
            self.log_observability_failure(
                "metrics.record_segment",
                exc,
                segment=segment,
            )

    def record_error_metric(self, attributes: dict[str, str]) -> None:
        try:
            self.errors.add(1, attributes)
        except BaseException as exc:
            self.log_observability_failure("metrics.record_error", exc)

    def record_rate_limited_metric(self, attributes: dict[str, str]) -> None:
        try:
            self.rate_limited.add(1, attributes)
        except BaseException as exc:
            self.log_observability_failure("metrics.record_rate_limited", exc)

    def record_failover_event_metric(self, attributes: dict[str, str]) -> None:
        try:
            self.failover_events.add(1, attributes)
        except BaseException as exc:
            self.log_observability_failure(
                "metrics.record_failover_event",
                exc,
            )

    def record_active_request(
        self,
        delta: int,
        attributes: dict[str, str],
    ) -> None:
        try:
            self.active_requests.add(delta, attributes)
        except BaseException as exc:
            self.log_observability_failure("metrics.add_active_request", exc)

    def shutdown_tracing(self) -> None:
        if self.tracer_provider is None:
            return

        def flush() -> None:
            try:
                self.tracer_provider.shutdown()
            except BaseException as exc:
                self.log_observability_failure("otel.shutdown", exc)

        thread = threading.Thread(
            target=flush,
            name="policyengine-otel-shutdown",
            daemon=True,
        )
        thread.start()
        thread.join(timeout=self.config.shutdown_timeout_seconds)
        if thread.is_alive():
            self.log_observability_failure(
                "otel.shutdown_timeout",
                TimeoutError("OpenTelemetry shutdown timed out."),
                timeout_seconds=self.config.shutdown_timeout_seconds,
            )

    def log_observability_failure(
        self,
        operation: str,
        exc: BaseException,
        **fields: Any,
    ) -> None:
        payload = {
            "schema_version": "policyengine.observability.internal_error.v1",
            "event": "observability_internal_error",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "error": {
                "type": type(exc).__name__,
                "message": self._safe_str(exc),
                "stack": self._safe_traceback(exc),
            },
        }
        payload.update(
            {key: value for key, value in fields.items() if value is not None}
        )
        try:
            INTERNAL_LOGGER.error(self._json(payload))
        except BaseException:
            self._write_stderr(payload)

    def _configure_loggers(self) -> None:
        for logger in (REQUEST_LOGGER, EVENT_LOGGER, INTERNAL_LOGGER):
            configure_plain_logger(logger, self.config.log_level)

    def _configure_otel(self) -> None:
        try:
            from opentelemetry import metrics
            from opentelemetry import propagate
            from opentelemetry import trace
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT
            from opentelemetry.sdk.resources import SERVICE_NAME
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.trace import SpanKind
            from opentelemetry.trace import Status
            from opentelemetry.trace import StatusCode
        except BaseException as exc:
            self.log_observability_failure("otel.configure_imports", exc)
            return

        try:
            resource = Resource.create(
                {
                    SERVICE_NAME: self.config.service_name,
                    DEPLOYMENT_ENVIRONMENT: self.config.environment,
                    "service.role": self.config.service_role,
                }
            )
            tracer_provider = TracerProvider(resource=resource)
            metric_readers = []
            if self.config.otlp_endpoint:
                self._add_trace_exporter(tracer_provider)
                metric_reader = self._metric_reader()
                if metric_reader is not None:
                    metric_readers.append(metric_reader)
            self.tracer_provider = tracer_provider
            try:
                trace.set_tracer_provider(tracer_provider)
            except BaseException as exc:
                self.log_observability_failure(
                    "otel.set_tracer_provider",
                    exc,
                )
            try:
                self.meter_provider = MeterProvider(
                    resource=resource,
                    metric_readers=metric_readers,
                )
                metrics.set_meter_provider(self.meter_provider)
            except BaseException as exc:
                self.log_observability_failure(
                    "otel.set_meter_provider",
                    exc,
                )
            self.trace = trace
            self.propagate = propagate
            self.SpanKind = SpanKind
            self.Status = Status
            self.StatusCode = StatusCode
            tracer_name = self.config.tracer_name or self.config.service_name
            meter_name = self.config.meter_name or self.config.service_name
            self.tracer = trace.get_tracer(tracer_name)
            self.meter = metrics.get_meter(meter_name)
            self._configure_instruments()
        except BaseException as exc:
            self.log_observability_failure("otel.configure", exc)

    def _add_trace_exporter(self, tracer_provider) -> None:
        try:
            if self.config.otlp_protocol.startswith("http"):
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
            else:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter())
            )
        except BaseException as exc:
            self.log_observability_failure("otel.trace_exporter", exc)

    def _metric_reader(self):
        try:
            if self.config.otlp_protocol.startswith("http"):
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )
            else:
                from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                    OTLPMetricExporter,
                )
            from opentelemetry.sdk.metrics.export import (
                PeriodicExportingMetricReader,
            )

            return PeriodicExportingMetricReader(OTLPMetricExporter())
        except BaseException as exc:
            self.log_observability_failure("otel.metric_exporter", exc)
            return None

    def _configure_instruments(self) -> None:
        self.http_duration = self._instrument(
            getattr(self.meter, "create_histogram", None),
            "http.server.request.duration",
            unit="s",
            description="HTTP server request duration.",
        )
        self.segment_duration = self._instrument(
            getattr(self.meter, "create_histogram", None),
            "policyengine.segment.duration",
            unit="s",
            description="PolicyEngine operation segment duration.",
        )
        self.calculate_duration = self._instrument(
            getattr(self.meter, "create_histogram", None),
            "policyengine.calculate.duration",
            unit="s",
            description="PolicyEngine calculate operation duration.",
        )
        self.backend_duration = self._instrument(
            getattr(self.meter, "create_histogram", None),
            "policyengine.backend.duration",
            unit="s",
            description="PolicyEngine backend call duration.",
        )
        self.requests = self._instrument(
            getattr(self.meter, "create_counter", None),
            "policyengine.requests",
            description="PolicyEngine request count.",
        )
        self.errors = self._instrument(
            getattr(self.meter, "create_counter", None),
            "policyengine.errors",
            description="PolicyEngine error count.",
        )
        self.rate_limited = self._instrument(
            getattr(self.meter, "create_counter", None),
            "policyengine.rate_limited_requests",
            description="PolicyEngine rate-limited request count.",
        )
        self.failover_events = self._instrument(
            getattr(self.meter, "create_counter", None),
            "policyengine.failover.events",
            description="PolicyEngine failover event count.",
        )
        self.active_requests = self._instrument(
            getattr(self.meter, "create_up_down_counter", None),
            "http.server.active_requests",
            description="Active HTTP server requests.",
        )

    def _instrument(self, factory, *args, **kwargs):
        if factory is None:
            return _NoOpInstrument()
        try:
            return factory(*args, **kwargs)
        except BaseException as exc:
            self.log_observability_failure(
                "metrics.create_instrument",
                exc,
                instrument=args[0] if args else None,
            )
            return _NoOpInstrument()

    def _start_request_span(
        self,
        context: RequestObservabilityContext,
        *,
        carrier: Any = None,
    ) -> None:
        if self.tracer is None:
            return
        attrs = context.span_attributes()
        parent_context = self._extract_context(carrier)
        try:
            context.server_span_cm = self.tracer.start_as_current_span(
                context.route,
                context=parent_context,
                kind=self.SpanKind.SERVER if self.SpanKind else None,
                attributes=attrs,
            )
            context.server_span = context.server_span_cm.__enter__()
        except BaseException as exc:
            context.server_span_cm = None
            context.server_span = None
            self.log_observability_failure("otel.request_span_enter", exc)

    def _close_request_span(
        self,
        context: RequestObservabilityContext,
        exc: BaseException | None,
    ) -> None:
        if context.span_closed:
            return
        context.span_closed = True
        span_cm = context.server_span_cm
        if span_cm is None:
            return
        try:
            if exc is None:
                span_cm.__exit__(None, None, None)
            else:
                span_cm.__exit__(type(exc), exc, exc.__traceback__)
        except BaseException as observability_exc:
            self.log_observability_failure(
                "otel.request_span_exit",
                observability_exc,
                request_id=context.request_id,
            )

    @contextmanager
    def _safe_span(self, name: str, attrs: dict[str, Any]) -> Iterator[Any]:
        if self.tracer is None:
            yield None
            return
        span_handle = self._start_span(name, attrs)
        if span_handle is None:
            yield None
            return
        _cm, span = span_handle
        try:
            yield span
        except BaseException as exc:
            try:
                self._end_span(span_handle, exc)
            except BaseException as observability_exc:
                self.log_observability_failure(
                    "otel.span_exit",
                    observability_exc,
                    span=name,
                )
            raise
        else:
            try:
                self._end_span(span_handle)
            except BaseException as exc:
                self.log_observability_failure(
                    "otel.span_exit",
                    exc,
                    span=name,
                )

    def _start_span(self, name: str, attrs: dict[str, Any]):
        try:
            span_cm = self.tracer.start_as_current_span(name)
            span = span_cm.__enter__()
            for key, value in attrs.items():
                if value is not None:
                    span.set_attribute(key, value)
            return span_cm, span
        except BaseException as exc:
            self.log_observability_failure("otel.span_enter", exc, span=name)
            return None

    def _end_span(
        self,
        span_handle,
        error: BaseException | None = None,
    ) -> None:
        if span_handle is None:
            return
        span_cm, span = span_handle
        try:
            if error is not None:
                self._record_exception_on_span(
                    span,
                    error,
                    handled=False,
                    status_code=500,
                )
        except BaseException as exc:
            self.log_observability_failure("otel.span_error_status", exc)
        try:
            span_cm.__exit__(None, None, None)
        except BaseException as exc:
            self.log_observability_failure("otel.span_exit", exc)

    def _record_segment_safely(
        self,
        name: str,
        start: float | None,
        attrs: dict[str, Any],
    ) -> None:
        if start is None:
            return
        end = self._safe_perf_counter(f"segment.{name}.end")
        if end is None:
            return
        try:
            duration = end - start
            self._record_timing(name, duration)
            context = self.current_context()
            if context is not None:
                context.timings_ms[name] = round(duration * 1000, 3)
                metric_extra = {
                    key: value
                    for key, value in attrs.items()
                    if key in METRIC_ATTRIBUTE_KEYS and value is not None
                }
                self.record_segment_metric(
                    name,
                    duration,
                    context.metric_attributes(
                        segment=name,
                        **metric_extra,
                    ),
                    backend_segment="backend" in metric_extra,
                )
        except BaseException as exc:
            self.log_observability_failure(
                "request.record_segment",
                exc,
                segment=name,
            )

    def _record_timing(self, name: str, duration_seconds: float) -> None:
        try:
            timings = _TIMINGS.get()
            if timings is None:
                return
            key = f"{name}_ms"
            duration_ms = duration_seconds * 1000.0
            timings[key] = round(timings.get(key, 0.0) + duration_ms, 1)
        except BaseException as exc:
            self.log_observability_failure(
                "scope.record_timing",
                exc,
                segment=name,
            )

    def _segment_span_attributes(
        self,
        attrs: dict[str, Any],
    ) -> dict[str, Any]:
        context = self.current_context()
        span_attrs = {
            key: value for key, value in attrs.items() if value is not None
        }
        if context is not None:
            span_attrs = {**context.span_attributes(), **span_attrs}
        return span_attrs

    def _span_name(self, segment_name: str) -> str:
        if not self.config.span_prefix:
            return segment_name
        return f"{self.config.span_prefix}.{segment_name}"

    def _coerce_segment(self, name: Any) -> str:
        segment, is_registered = coerce_segment_name(
            name,
            registry=self.segment_registry,
        )
        if not is_registered:
            self.log_observability_failure(
                "segment.coerce",
                ValueError("Unregistered observability segment."),
                segment=segment,
                segment_type=type(name).__name__,
            )
        return segment

    def _set_current_span_attributes(self, attrs: dict[str, Any]) -> None:
        span = self._current_span()
        if span is None:
            return
        try:
            for key, value in attrs.items():
                if value is not None:
                    span.set_attribute(key, value)
        except BaseException as exc:
            self.log_observability_failure("otel.set_span_attributes", exc)

    def _current_span(self):
        if self.trace is None:
            return None
        try:
            return self.trace.get_current_span()
        except BaseException as exc:
            self.log_observability_failure("otel.current_span", exc)
            return None

    def _trace_ids(self) -> tuple[str | None, str | None]:
        span = self._current_span()
        if span is None:
            return None, None
        try:
            context = span.get_span_context()
        except BaseException as exc:
            self.log_observability_failure("otel.span_context", exc)
            return None, None
        if not getattr(context, "is_valid", False):
            return None, None
        return f"{context.trace_id:032x}", f"{context.span_id:016x}"

    def _extract_context(self, carrier: Any):
        if self.propagate is None or carrier is None:
            return None
        try:
            return self.propagate.extract(carrier)
        except BaseException as exc:
            self.log_observability_failure("otel.extract_context", exc)
            return None

    def _record_exception_on_span(
        self,
        span,
        exc: BaseException,
        *,
        handled: bool,
        status_code: int | None,
    ) -> None:
        try:
            span.record_exception(exc)
            span.set_attribute("error.type", type(exc).__name__)
            span.set_attribute("error.handled", handled)
            if (
                self.Status is not None
                and self.StatusCode is not None
                and (
                    not handled
                    or (status_code is not None and status_code >= 500)
                )
            ):
                span.set_status(
                    self.Status(
                        self.StatusCode.ERROR,
                        self._safe_str(exc),
                    )
                )
        except BaseException as observability_exc:
            self.log_observability_failure(
                "otel.record_exception",
                observability_exc,
                original_error_type=type(exc).__name__,
            )

    def _add_span_event(self, event: str, fields: dict[str, Any]) -> None:
        span = self._current_span()
        if span is None:
            return
        try:
            span.add_event(
                event,
                {
                    key: value
                    for key, value in fields.items()
                    if _is_safe_span_value(value)
                },
            )
        except BaseException as exc:
            self.log_observability_failure(
                "otel.add_event",
                exc,
                event_name=event,
            )

    def _close_active_request(
        self,
        context: RequestObservabilityContext,
    ) -> None:
        try:
            if context.active_closed:
                return
            context.active_closed = True
            self.record_active_request(-1, context.metric_attributes())
        except BaseException as exc:
            self.log_observability_failure(
                "request.close_active",
                exc,
                request_id=getattr(context, "request_id", None),
            )

    def _reset_request_context(
        self,
        context: RequestObservabilityContext,
    ) -> None:
        token = context.context_token
        if token is None:
            return
        try:
            _REQUEST_CONTEXT.reset(token)
        except BaseException as exc:
            self.log_observability_failure(
                "request.context_reset",
                exc,
                request_id=getattr(context, "request_id", None),
            )

    def _safe_perf_counter(self, operation: str) -> float | None:
        try:
            return time.perf_counter()
        except BaseException as exc:
            self.log_observability_failure(operation, exc)
            return None

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
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "operation": "observability.failure_json",
                },
                sort_keys=True,
            )

    def _write_stderr(self, payload: dict[str, Any]) -> None:
        try:
            sys.stderr.write(self._json(payload) + "\n")
        except BaseException:
            return


def _is_safe_span_value(value: Any) -> bool:
    return isinstance(value, str | bool | int | float)


_RUNTIME = ObservabilityRuntime(ObservabilityConfig())


def set_observability_runtime(runtime: ObservabilityRuntime) -> None:
    global _RUNTIME
    _RUNTIME = runtime


def observability_runtime() -> ObservabilityRuntime:
    return _RUNTIME
