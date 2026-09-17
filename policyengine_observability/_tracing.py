"""Trace initialization, propagation, and span lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ._state import TRACEPARENT_HEADER
from .context import (
    RequestObservabilityContext,
)

if TYPE_CHECKING:
    from .runtime import ObservabilityRuntime


def _is_safe_span_value(value: Any) -> bool:
    return isinstance(value, str | bool | int | float)


class TraceRecorder:
    def __init__(self, runtime: ObservabilityRuntime) -> None:
        self.runtime = runtime

    def traceparent_header(self) -> str | None:
        if not self.runtime.enabled or self.runtime.propagate is None:
            return None
        try:
            carrier: dict[str, str] = {}
            self.runtime.propagate.inject(carrier)
            return carrier.get(TRACEPARENT_HEADER)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "request.traceparent_header", exc
            )
            return None

    def capture_context(self):
        if self.runtime.tracer is None:
            return None
        try:
            from opentelemetry import context as otel_context

            return otel_context.get_current()
        except BaseException as exc:
            self.runtime.log_observability_failure("otel.capture_context", exc)
            return None

    def instrument_fastapi(self, app: Any) -> None:
        if not self.runtime.enabled or not self.runtime.config.otel_enabled:
            return
        try:
            from opentelemetry.instrumentation.fastapi import (
                FastAPIInstrumentor,
            )

            FastAPIInstrumentor.instrument_app(app)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "fastapi.auto_instrument",
                exc,
            )

    def instrument_httpx(self) -> None:
        if (
            not self.runtime.enabled
            or not self.runtime.config.otel_enabled
            or self.runtime._httpx_instrumented
        ):
            return
        try:
            from opentelemetry.instrumentation.httpx import (
                HTTPXClientInstrumentor,
            )

            HTTPXClientInstrumentor().instrument()
            self.runtime._httpx_instrumented = True
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "httpx.auto_instrument", exc
            )

    def _configure_otel(self) -> None:
        try:
            from opentelemetry import metrics, propagate, trace
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.resources import (
                DEPLOYMENT_ENVIRONMENT,
                SERVICE_NAME,
                Resource,
            )
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.trace import SpanKind, Status, StatusCode
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "otel.configure_imports", exc
            )
            return

        try:
            resource = Resource.create(
                {
                    SERVICE_NAME: self.runtime.config.service_name,
                    DEPLOYMENT_ENVIRONMENT: self.runtime.config.environment,
                    "service.role": self.runtime.config.service_role,
                }
            )
            tracer_provider = TracerProvider(resource=resource)
            metric_readers = []
            if self.runtime.config.otlp_endpoint:
                self.runtime._add_trace_exporter(tracer_provider)
                metric_reader = self.runtime._metric_reader()
                if metric_reader is not None:
                    metric_readers.append(metric_reader)
            self.runtime.tracer_provider = tracer_provider
            try:
                trace.set_tracer_provider(tracer_provider)
            except BaseException as exc:
                self.runtime.log_observability_failure(
                    "otel.set_tracer_provider",
                    exc,
                )
            try:
                self.runtime.meter_provider = MeterProvider(
                    resource=resource,
                    metric_readers=metric_readers,
                )
                metrics.set_meter_provider(self.runtime.meter_provider)
            except BaseException as exc:
                self.runtime.log_observability_failure(
                    "otel.set_meter_provider",
                    exc,
                )
            self.runtime.trace = trace
            self.runtime.propagate = propagate
            self.runtime.SpanKind = SpanKind
            self.runtime.Status = Status
            self.runtime.StatusCode = StatusCode
            tracer_name = (
                self.runtime.config.tracer_name
                or self.runtime.config.service_name
            )
            meter_name = (
                self.runtime.config.meter_name
                or self.runtime.config.service_name
            )
            self.runtime.tracer = trace.get_tracer(tracer_name)
            self.runtime.meter = metrics.get_meter(meter_name)
            self.runtime._configure_instruments()
        except BaseException as exc:
            self.runtime.log_observability_failure("otel.configure", exc)

    def _add_trace_exporter(self, tracer_provider) -> None:
        try:
            if self.runtime.config.otlp_protocol.startswith("http"):
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
            self.runtime.log_observability_failure("otel.trace_exporter", exc)

    def _metric_reader(self):
        try:
            if self.runtime.config.otlp_protocol.startswith("http"):
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
            self.runtime.log_observability_failure("otel.metric_exporter", exc)
            return None

    def _start_request_span(
        self,
        context: RequestObservabilityContext,
        *,
        carrier: Any = None,
    ) -> None:
        if self.runtime.tracer is None:
            return
        attrs = context.span_attributes()
        parent_context = self.runtime._extract_context(carrier)
        try:
            context.server_span_cm = self.runtime.tracer.start_as_current_span(
                context.route,
                context=parent_context,
                kind=self.runtime.SpanKind.SERVER
                if self.runtime.SpanKind
                else None,
                attributes=attrs,
            )
            context.server_span = context.server_span_cm.__enter__()
        except BaseException as exc:
            context.server_span_cm = None
            context.server_span = None
            self.runtime.log_observability_failure(
                "otel.request_span_enter", exc
            )

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
            self.runtime.log_observability_failure(
                "otel.request_span_exit",
                observability_exc,
                request_id=context.request_id,
            )

    @contextmanager
    def _safe_span(self, name: str, attrs: dict[str, Any]) -> Iterator[Any]:
        if self.runtime.tracer is None:
            yield None
            return
        span_handle = self.runtime._start_span(name, attrs)
        if span_handle is None:
            yield None
            return
        _cm, span = span_handle
        try:
            yield span
        except BaseException as exc:
            try:
                self.runtime._end_span(span_handle, exc)
            except BaseException as observability_exc:
                self.runtime.log_observability_failure(
                    "otel.span_exit",
                    observability_exc,
                    span=name,
                )
            raise
        else:
            try:
                self.runtime._end_span(span_handle)
            except BaseException as exc:
                self.runtime.log_observability_failure(
                    "otel.span_exit",
                    exc,
                    span=name,
                )

    def _start_span(self, name: str, attrs: dict[str, Any]):
        try:
            span_cm = self.runtime.tracer.start_as_current_span(name)
            span = span_cm.__enter__()
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "otel.span_enter", exc, span=name
            )
            return None
        try:
            for key, value in attrs.items():
                if value is not None:
                    span.set_attribute(key, value)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "otel.span_attributes",
                exc,
                span=name,
            )
        return span_cm, span

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
                self.runtime._record_exception_on_span(
                    span,
                    error,
                    handled=False,
                    status_code=500,
                )
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "otel.span_error_status", exc
            )
        try:
            span_cm.__exit__(None, None, None)
        except BaseException as exc:
            self.runtime.log_observability_failure("otel.span_exit", exc)

    def _segment_span_attributes(
        self,
        attrs: dict[str, Any],
    ) -> dict[str, Any]:
        context = self.runtime.current_context()
        operation = self.runtime.current_operation()
        span_attrs = {
            key: value for key, value in attrs.items() if value is not None
        }
        if context is not None:
            span_attrs = {**context.span_attributes(), **span_attrs}
        elif operation is not None:
            span_attrs = {**operation.span_attributes(), **span_attrs}
        return span_attrs

    def _span_name(self, segment_name: str) -> str:
        if not self.runtime.config.span_prefix:
            return segment_name
        return f"{self.runtime.config.span_prefix}.{segment_name}"

    def _set_current_span_attributes(self, attrs: dict[str, Any]) -> None:
        span = self.runtime._current_span()
        if span is None:
            return
        try:
            for key, value in attrs.items():
                if value is not None:
                    span.set_attribute(key, value)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "otel.set_span_attributes", exc
            )

    def _current_span(self):
        if self.runtime.trace is None:
            return None
        try:
            return self.runtime.trace.get_current_span()
        except BaseException as exc:
            self.runtime.log_observability_failure("otel.current_span", exc)
            return None

    def _trace_ids(self) -> tuple[str | None, str | None]:
        span = self.runtime._current_span()
        if span is None:
            return None, None
        try:
            context = span.get_span_context()
        except BaseException as exc:
            self.runtime.log_observability_failure("otel.span_context", exc)
            return None, None
        if not getattr(context, "is_valid", False):
            return None, None
        return f"{context.trace_id:032x}", f"{context.span_id:016x}"

    def _extract_context(self, carrier: Any):
        if self.runtime.propagate is None or carrier is None:
            return None
        try:
            return self.runtime.propagate.extract(carrier)
        except BaseException as exc:
            self.runtime.log_observability_failure("otel.extract_context", exc)
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
                self.runtime.Status is not None
                and self.runtime.StatusCode is not None
                and (
                    not handled
                    or (status_code is not None and status_code >= 500)
                )
            ):
                span.set_status(
                    self.runtime.Status(
                        self.runtime.StatusCode.ERROR,
                        self.runtime._safe_str(exc),
                    )
                )
        except BaseException as observability_exc:
            self.runtime.log_observability_failure(
                "otel.record_exception",
                observability_exc,
                original_error_type=type(exc).__name__,
            )

    def _add_span_event(self, event: str, fields: dict[str, Any]) -> None:
        span = self.runtime._current_span()
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
            self.runtime.log_observability_failure(
                "otel.add_event",
                exc,
                event_name=event,
            )
