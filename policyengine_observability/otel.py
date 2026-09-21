from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlparse

from .config import ObservabilityConfig, OTelConfig
from .diagnostics import Diagnostics
from .schema import metric_attributes


@dataclass(slots=True)
class SpanHandle:
    manager: Any
    span: Any


class OTelRuntime:
    def __init__(
        self,
        config: ObservabilityConfig,
        diagnostics: Diagnostics,
        *,
        queue_depth: Callable[[], int] | None = None,
    ) -> None:
        self.config = config
        self.diagnostics = diagnostics
        self._queue_depth_callback = queue_depth or (lambda: 0)
        self.tracer: Any | None = None
        self.meter: Any | None = None
        self._tracer_provider: Any | None = None
        self._meter_provider: Any | None = None
        self._owns_tracer_provider = False
        self._owns_meter_provider = False
        self._request_count: Any | None = None
        self._request_duration: Any | None = None
        self._operation_count: Any | None = None
        self._operation_duration: Any | None = None
        self._error_count: Any | None = None
        self._dropped_count: Any | None = None
        self._exporter_failure_count: Any | None = None
        self._queue_depth: Any | None = None
        self._configure()

    def _configure(self) -> None:
        if not self.config.otel.enabled or not self.config.identity_complete:
            return
        try:
            from opentelemetry import metrics, trace

            if self.config.otel.provider_mode == "external":
                self._tracer_provider = trace.get_tracer_provider()
                self._meter_provider = metrics.get_meter_provider()
            else:
                self._configure_owned_providers()
            if self._tracer_provider is not None:
                self.tracer = self._tracer_provider.get_tracer(
                    "policyengine-observability", "2.0.0"
                )
            if self._meter_provider is not None:
                self.meter = self._meter_provider.get_meter(
                    "policyengine-observability", "2.0.0"
                )
                self._configure_instruments()
        except Exception as exc:
            self.diagnostics.report("otel.configure", exc)
            self.tracer = None
            self.meter = None

    def _configure_owned_providers(self) -> None:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.sampling import (
            ParentBased,
            TraceIdRatioBased,
        )

        resource = Resource.create(self.resource_attributes())
        readers: list[Any] = []
        otel = self.config.otel
        if otel.endpoint and otel.metrics_enabled:
            from opentelemetry.sdk.metrics.export import (
                PeriodicExportingMetricReader,
            )

            readers.append(
                PeriodicExportingMetricReader(
                    _lazy_metric_exporter_class()(
                        lambda: _build_metric_exporter(otel),
                        self.diagnostics,
                    ),
                    export_interval_millis=max(
                        1_000,
                        otel.metric_export_interval_seconds * 1_000,
                    ),
                    export_timeout_millis=max(
                        1,
                        otel.export_timeout_seconds * 1_000,
                    ),
                )
            )
        self._meter_provider = MeterProvider(
            metric_readers=readers,
            resource=resource,
            shutdown_on_exit=False,
        )
        self._owns_meter_provider = True

        self._tracer_provider = TracerProvider(
            sampler=ParentBased(
                TraceIdRatioBased(min(max(otel.sampling_ratio, 0.0), 1.0))
            ),
            resource=resource,
            shutdown_on_exit=False,
            meter_provider=self._meter_provider,
        )
        self._owns_tracer_provider = True
        if otel.endpoint and otel.traces_enabled:
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            batch_size = min(
                max(1, otel.span_batch_size),
                max(1, otel.span_queue_capacity),
            )
            self._tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    cast(
                        Any,
                        _LazySpanExporter(
                            lambda: _build_span_exporter(otel),
                            self.diagnostics,
                        ),
                    ),
                    max_queue_size=max(1, otel.span_queue_capacity),
                    max_export_batch_size=batch_size,
                    schedule_delay_millis=max(
                        1,
                        otel.span_schedule_delay_seconds * 1_000,
                    ),
                    export_timeout_millis=max(
                        1,
                        otel.export_timeout_seconds * 1_000,
                    ),
                    meter_provider=self._meter_provider,
                )
            )

    def _configure_instruments(self) -> None:
        meter = self.meter
        if meter is None:
            return
        self._request_count = meter.create_counter(
            "policyengine.request.count"
        )
        self._request_duration = meter.create_histogram(
            "policyengine.request.duration",
            unit="s",
        )
        self._operation_count = meter.create_counter(
            "policyengine.operation.count"
        )
        self._operation_duration = meter.create_histogram(
            "policyengine.operation.duration",
            unit="s",
        )
        self._error_count = meter.create_counter("policyengine.error.count")
        self._dropped_count = meter.create_counter(
            "policyengine.telemetry.dropped"
        )
        self._exporter_failure_count = meter.create_counter(
            "policyengine.telemetry.exporter.failure"
        )
        self._queue_depth = meter.create_observable_gauge(
            "policyengine.telemetry.queue.depth",
            callbacks=[self._observe_queue_depth],
        )

    def _observe_queue_depth(self, _options: Any) -> list[Any]:
        try:
            from opentelemetry.metrics import Observation

            return [Observation(max(0, int(self._queue_depth_callback())))]
        except Exception as exc:
            self.diagnostics.report("otel.queue_depth", exc)
            return []

    def resource_attributes(self) -> dict[str, str]:
        service = self.config.service
        deployment = self.config.deployment
        values = {
            "service.name": service.name,
            "service.namespace": service.namespace,
            "service.version": service.version,
            "service.instance.id": deployment.instance_id,
            "deployment.environment.name": deployment.environment,
            "cloud.platform": deployment.platform,
            "cloud.region": deployment.region,
            "policyengine.service.role": service.role,
        }
        return {key: value for key, value in values.items() if value}

    def start_span(
        self,
        name: str,
        *,
        kind: Any = None,
        attributes: Mapping[str, Any] | None = None,
        parent_context: Any = None,
        links: Sequence[Any] | None = None,
    ) -> SpanHandle | None:
        if self.tracer is None:
            return None
        try:
            kwargs: dict[str, Any] = {
                "attributes": dict(attributes or {}),
                "record_exception": False,
                "set_status_on_exception": False,
            }
            if kind is not None:
                kwargs["kind"] = kind
            if parent_context is not None:
                kwargs["context"] = parent_context
            if links:
                kwargs["links"] = list(links)
            manager = self.tracer.start_as_current_span(name, **kwargs)
            return SpanHandle(manager=manager, span=manager.__enter__())
        except Exception as exc:
            self.diagnostics.report("otel.span_start", exc, span=name)
            return None

    def end_span(
        self,
        handle: SpanHandle | None,
        error: BaseException | None = None,
    ) -> None:
        if handle is None:
            return
        try:
            if error is not None:
                from opentelemetry.trace import Status, StatusCode

                if isinstance(error, Exception):
                    handle.span.record_exception(error)
                handle.span.set_status(Status(StatusCode.ERROR))
            handle.manager.__exit__(
                type(error) if error is not None else None,
                error,
                error.__traceback__ if error is not None else None,
            )
        except Exception as exc:
            self.diagnostics.report("otel.span_end", exc)

    def set_span_attributes(self, values: Mapping[str, Any]) -> None:
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            if not span.is_recording():
                return
            for key, value in values.items():
                span.set_attribute(key, value)
        except Exception as exc:
            self.diagnostics.report("otel.span_attribute", exc)

    def current_correlation(self) -> dict[str, Any]:
        try:
            from opentelemetry import trace

            span_context = trace.get_current_span().get_span_context()
            if not span_context.is_valid:
                return {}
            return {
                "trace_id": format(span_context.trace_id, "032x"),
                "span_id": format(span_context.span_id, "016x"),
                "trace_sampled": bool(span_context.trace_flags.sampled),
            }
        except Exception:
            return {}

    def extract(self, carrier: Mapping[str, str]) -> Any:
        try:
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )

            return TraceContextTextMapPropagator().extract(carrier=carrier)
        except Exception as exc:
            self.diagnostics.report("otel.context_extract", exc)
            return None

    def inject(self, carrier: dict[str, str]) -> None:
        try:
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )

            TraceContextTextMapPropagator().inject(carrier=carrier)
        except Exception as exc:
            self.diagnostics.report("otel.context_inject", exc)

    def remote_span_context(self, carrier: Mapping[str, str]) -> Any | None:
        try:
            from opentelemetry import trace

            context = self.extract(carrier)
            if context is None:
                return None
            span_context = trace.get_current_span(context).get_span_context()
            return span_context if span_context.is_valid else None
        except Exception:
            return None

    def link(self, span_context: Any) -> Any | None:
        if span_context is None:
            return None
        try:
            from opentelemetry.trace import Link

            return Link(span_context)
        except Exception:
            return None

    def empty_context(self) -> Any | None:
        try:
            from opentelemetry.context import Context

            return Context()
        except Exception:
            return None

    def record_request(
        self,
        duration_seconds: float,
        attributes: Mapping[str, Any],
    ) -> None:
        bounded = metric_attributes(attributes, self.config)
        self._safe_metric(self._request_count, "add", 1, bounded)
        self._safe_metric(
            self._request_duration,
            "record",
            duration_seconds,
            bounded,
        )

    def record_operation(
        self,
        duration_seconds: float,
        attributes: Mapping[str, Any],
    ) -> None:
        bounded = metric_attributes(attributes, self.config)
        self._safe_metric(self._operation_count, "add", 1, bounded)
        self._safe_metric(
            self._operation_duration,
            "record",
            duration_seconds,
            bounded,
        )

    def record_error(self, attributes: Mapping[str, Any]) -> None:
        self._safe_metric(
            self._error_count,
            "add",
            1,
            metric_attributes(attributes, self.config),
        )

    def record_dropped(self, kind: str, count: int = 1) -> None:
        self._safe_metric(
            self._dropped_count,
            "add",
            count,
            {"operation.kind": kind},
        )

    def record_exporter_failure(self, kind: str, count: int = 1) -> None:
        self._safe_metric(
            self._exporter_failure_count,
            "add",
            count,
            {"operation.kind": kind},
        )

    def _safe_metric(
        self,
        instrument: Any | None,
        method: str,
        value: int | float,
        attributes: Mapping[str, Any],
    ) -> None:
        if instrument is None:
            return
        try:
            getattr(instrument, method)(value, attributes=dict(attributes))
        except Exception as exc:
            self.diagnostics.report("otel.metric_record", exc)

    def force_flush(self, timeout_seconds: float) -> None:
        timeout_ms = max(0, int(timeout_seconds * 1_000))
        for provider in (self._tracer_provider, self._meter_provider):
            force_flush = getattr(provider, "force_flush", None)
            if callable(force_flush):
                try:
                    force_flush(timeout_millis=timeout_ms)
                except Exception as exc:
                    self.diagnostics.report("otel.force_flush", exc)

    def shutdown(self, timeout_seconds: float) -> None:
        timeout_ms = max(0, int(timeout_seconds * 1_000))
        if self._owns_tracer_provider and self._tracer_provider is not None:
            try:
                self._tracer_provider.shutdown()
            except Exception as exc:
                self.diagnostics.report("otel.trace_shutdown", exc)
        if self._owns_meter_provider and self._meter_provider is not None:
            try:
                self._meter_provider.shutdown(timeout_millis=timeout_ms)
            except Exception as exc:
                self.diagnostics.report("otel.metric_shutdown", exc)


class _LazySpanExporter:
    def __init__(
        self,
        factory: Callable[[], Any],
        diagnostics: Diagnostics,
    ) -> None:
        self._factory = factory
        self._diagnostics = diagnostics
        self._delegate: Any | None = None
        self._lock = threading.Lock()

    def export(self, spans: Sequence[Any]) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            return self._get().export(spans)
        except Exception as exc:
            self._diagnostics.increment("spans.export_failure")
            self._diagnostics.report("otel.span_export", exc)
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            delegate = self._delegate
            return (
                True
                if delegate is None
                else bool(delegate.force_flush(timeout_millis))
            )
        except Exception as exc:
            self._diagnostics.report("otel.span_flush", exc)
            return False

    def shutdown(self) -> None:
        try:
            if self._delegate is not None:
                self._delegate.shutdown()
        except Exception as exc:
            self._diagnostics.report("otel.span_exporter_shutdown", exc)

    def _get(self) -> Any:
        if self._delegate is None:
            with self._lock:
                if self._delegate is None:
                    self._delegate = self._factory()
        return self._delegate


def _lazy_metric_exporter_class():
    from opentelemetry.sdk.metrics.export import MetricExporter

    class LazyMetricExporter(MetricExporter):
        def __init__(
            self,
            factory: Callable[[], Any],
            diagnostics: Diagnostics,
        ) -> None:
            super().__init__()
            self._factory = factory
            self._diagnostics = diagnostics
            self._delegate: Any | None = None
            self._lock = threading.Lock()

        def export(
            self,
            metrics_data: Any,
            timeout_millis: float = 10_000,
            **kwargs: Any,
        ) -> Any:
            from opentelemetry.sdk.metrics.export import MetricExportResult

            try:
                return self._get().export(
                    metrics_data,
                    timeout_millis=timeout_millis,
                    **kwargs,
                )
            except Exception as exc:
                self._diagnostics.increment("metrics.export_failure")
                self._diagnostics.report("otel.metric_export", exc)
                return MetricExportResult.FAILURE

        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            try:
                delegate = self._delegate
                return (
                    True
                    if delegate is None
                    else bool(delegate.force_flush(timeout_millis))
                )
            except Exception as exc:
                self._diagnostics.report("otel.metric_flush", exc)
                return False

        def shutdown(
            self, timeout_millis: float = 30_000, **kwargs: Any
        ) -> None:
            try:
                if self._delegate is not None:
                    self._delegate.shutdown(
                        timeout_millis=timeout_millis,
                        **kwargs,
                    )
            except Exception as exc:
                self._diagnostics.report("otel.metric_exporter_shutdown", exc)

        def _get(self) -> Any:
            if self._delegate is None:
                with self._lock:
                    if self._delegate is None:
                        self._delegate = self._factory()
            return self._delegate

    return LazyMetricExporter


def _build_span_exporter(config: OTelConfig) -> Any:
    kwargs = _exporter_kwargs(config)
    if config.protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        kwargs["endpoint"] = _http_signal_endpoint(
            config.endpoint or "", "traces"
        )
        return OTLPSpanExporter(**kwargs)
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )

    return OTLPSpanExporter(**kwargs)


def _build_metric_exporter(config: OTelConfig) -> Any:
    kwargs = _exporter_kwargs(config)
    if config.protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )

        kwargs["endpoint"] = _http_signal_endpoint(
            config.endpoint or "", "metrics"
        )
        return OTLPMetricExporter(**kwargs)
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,
    )

    return OTLPMetricExporter(**kwargs)


def _exporter_kwargs(config: OTelConfig) -> dict[str, Any]:
    headers = dict(config.headers)
    kwargs: dict[str, Any] = {
        "endpoint": config.endpoint,
        "headers": headers,
        "timeout": max(0.1, min(config.export_timeout_seconds, 60.0)),
    }
    if config.google_audience:
        if config.protocol == "grpc":
            kwargs["credentials"] = _google_grpc_credentials(
                config.google_audience
            )
        else:
            kwargs["session"] = _google_http_session(
                config.google_audience,
                headers,
            )
            kwargs.pop("headers", None)
    return kwargs


def _google_id_token_credentials(audience: str) -> Any:
    from google.auth.transport.requests import Request

    modal_token = os.getenv("MODAL_IDENTITY_TOKEN") or os.getenv(
        "OBSERVABILITY_GOOGLE_OIDC_TOKEN"
    )
    provider = os.getenv("OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER")
    service_account = os.getenv("OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL")
    if modal_token and provider and service_account:
        from google.auth import identity_pool, impersonated_credentials

        source = identity_pool.Credentials.from_info(
            {
                "type": "external_account",
                "audience": _workload_identity_audience(provider),
                "subject_token_type": ("urn:ietf:params:oauth:token-type:jwt"),
                "token_url": "https://sts.googleapis.com/v1/token",
                "credential_source": {
                    "file": _write_subject_token(modal_token),
                    "format": {"type": "text"},
                },
            },
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        target = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=service_account,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return impersonated_credentials.IDTokenCredentials(
            target_credentials=target,
            target_audience=audience,
            include_email=True,
        )

    from google.oauth2.id_token import fetch_id_token_credentials

    return fetch_id_token_credentials(audience, request=Request())


def _google_grpc_credentials(audience: str) -> Any:
    import grpc
    from google.auth.transport.grpc import AuthMetadataPlugin
    from google.auth.transport.requests import Request

    plugin = AuthMetadataPlugin(
        _google_id_token_credentials(audience),
        Request(),
        default_host=urlparse(audience).netloc,
    )
    return grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(),
        grpc.metadata_call_credentials(plugin),
    )


def _google_http_session(audience: str, headers: Mapping[str, str]) -> Any:
    from google.auth.transport.requests import AuthorizedSession

    session = AuthorizedSession(_google_id_token_credentials(audience))
    session.headers.update(headers)
    return session


def _http_signal_endpoint(endpoint: str, signal: str) -> str:
    value = endpoint.rstrip("/")
    if value.endswith(f"/v1/{signal}"):
        return value
    return f"{value}/v1/{signal}"


def _workload_identity_audience(provider: str) -> str:
    value = provider.strip()
    if value.startswith("//iam.googleapis.com/"):
        return value
    if value.startswith("projects/"):
        return f"//iam.googleapis.com/{value}"
    return value


def _write_subject_token(token: str) -> str:
    import tempfile
    from pathlib import Path

    path = Path(tempfile.gettempdir()) / (
        "policyengine-observability-otel-oidc.jwt"
    )
    path.write_text(token)
    path.chmod(0o600)
    return str(path)


def captured_at_is_recent(value: Any, max_age_seconds: float) -> bool:
    if not isinstance(value, str):
        return False
    try:
        captured_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if captured_at.tzinfo is None:
        return False
    age = (datetime.now(UTC) - captured_at.astimezone(UTC)).total_seconds()
    return 0 <= age <= max_age_seconds
