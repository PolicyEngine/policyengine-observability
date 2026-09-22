from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime, timedelta

from conftest import make_config, records
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from policyengine_observability import (
    GoogleIdTokenAuth,
    OTelConfig,
    OTLPExporterConfig,
    configure,
)
from policyengine_observability.diagnostics import Diagnostics
from policyengine_observability.google_auth import (
    _google_grpc_credentials,
    _google_http_session,
    _google_id_token_credentials,
    _workload_identity_audience,
    _write_subject_token,
)
from policyengine_observability.otel import (
    OTelRuntime,
    SpanHandle,
    _build_metric_exporter,
    _build_span_exporter,
    _exporter_kwargs,
    _http_signal_endpoint,
    _lazy_metric_exporter_class,
    _LazySpanExporter,
    captured_at_is_recent,
)


def _runtime_with_spans():
    config = make_config(otel=OTelConfig(enabled=True))
    runtime = configure(config)
    runtime._delivery._stdout = io.StringIO()
    exporter = InMemorySpanExporter()
    runtime._otel._tracer_provider.add_span_processor(
        SimpleSpanProcessor(exporter)
    )
    return runtime, exporter


def test_owned_provider_creates_local_spans_metrics_and_resources() -> None:
    runtime, exporter = _runtime_with_spans()
    resource = runtime._otel.resource_attributes()
    assert resource["service.name"] == "test-api"
    assert resource["deployment.environment.name"] == "test"
    assert runtime._otel.tracer is not None
    assert runtime._otel.meter is not None
    with runtime.operation("simulation.run"):
        with runtime.span("simulation.calculate"):
            runtime.event("calculation.started")
    runtime._otel.force_flush(1)
    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {
        "simulation.run",
        "simulation.calculate",
    }
    child = next(span for span in spans if span.name == "simulation.calculate")
    parent = next(span for span in spans if span.name == "simulation.run")
    assert child.parent.span_id == parent.context.span_id
    runtime.shutdown()


def test_incoming_trace_context_correlates_log_and_response() -> None:
    runtime, exporter = _runtime_with_spans()
    trace_id = "1" * 32
    parent_id = "2" * 16
    runtime.begin_request(
        headers={"traceparent": f"00-{trace_id}-{parent_id}-01"},
        method="GET",
        route="/health",
    )
    headers = runtime.response_headers()
    assert headers["traceparent"].startswith(f"00-{trace_id}-")
    runtime.end_request(status_code=200)
    item = records(runtime._delivery._stdout)[0]
    assert item["trace_id"] == trace_id
    assert "logging.googleapis.com/trace" not in item
    span = exporter.get_finished_spans()[0]
    assert span.parent.span_id == int(parent_id, 16)
    runtime.shutdown()


def test_malformed_trace_context_starts_new_trace() -> None:
    runtime, exporter = _runtime_with_spans()
    runtime.begin_request(
        headers={"traceparent": "malformed"},
        method="GET",
        route="/health",
    )
    runtime.end_request(status_code=200)
    span = exporter.get_finished_spans()[0]
    assert span.context.trace_id != 0
    assert span.parent is None
    runtime.shutdown()


def test_recent_async_context_is_parent_and_old_context_is_link() -> None:
    runtime, exporter = _runtime_with_spans()
    trace_id = "3" * 32
    parent_id = "4" * 16
    recent = {
        "traceparent": f"00-{trace_id}-{parent_id}-01",
        "request_id": "dispatch-request",
        "captured_at": datetime.now(UTC).isoformat(),
    }
    with runtime.operation("recent", remote_context=recent):
        runtime.event("recent.event")

    old = {
        **recent,
        "captured_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
    }
    with runtime.operation("old", remote_context=old):
        pass
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["recent"].parent.span_id == int(parent_id, 16)
    assert spans["old"].parent is None
    assert spans["old"].links[0].context.span_id == int(parent_id, 16)
    runtime.shutdown()


def test_retry_forces_span_link_even_when_context_is_recent() -> None:
    runtime, exporter = _runtime_with_spans()
    remote = {
        "traceparent": f"00-{'5' * 32}-{'6' * 16}-01",
        "captured_at": datetime.now(UTC).isoformat(),
    }
    with runtime.operation(
        "retry", remote_context=remote, independent_retry=True
    ):
        pass
    span = exporter.get_finished_spans()[0]
    assert span.parent is None
    assert len(span.links) == 1
    runtime.shutdown()


def test_external_provider_mode_uses_caller_provider(monkeypatch) -> None:
    class Provider:
        def get_tracer(self, *_args):
            return object()

    class MeterProvider:
        def get_meter(self, *_args):
            return None

    tracer_provider = Provider()
    meter_provider = MeterProvider()
    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer_provider", lambda: tracer_provider
    )
    monkeypatch.setattr(
        "opentelemetry.metrics.get_meter_provider", lambda: meter_provider
    )
    otel = OTelRuntime(
        make_config(otel=OTelConfig(enabled=True, provider_mode="external")),
        Diagnostics(),
    )
    assert otel._tracer_provider is tracer_provider
    assert otel._meter_provider is meter_provider
    assert not otel._owns_tracer_provider


def test_lazy_span_exporter_contains_failure() -> None:
    diagnostics = Diagnostics()
    exporter = _LazySpanExporter(
        lambda: (_ for _ in ()).throw(ConnectionError("dns failure")),
        diagnostics,
    )
    assert exporter.export([]) is SpanExportResult.FAILURE
    assert diagnostics.count("spans.export_failure") == 1
    assert exporter.force_flush()
    exporter.shutdown()


def test_lazy_metric_exporter_contains_failure() -> None:
    from opentelemetry.sdk.metrics.export import MetricExportResult

    diagnostics = Diagnostics()
    exporter = _lazy_metric_exporter_class()(
        lambda: (_ for _ in ()).throw(PermissionError("denied")),
        diagnostics,
    )
    assert exporter.export(object()) is MetricExportResult.FAILURE
    assert diagnostics.count("metrics.export_failure") == 1
    assert exporter.force_flush()
    exporter.shutdown()


def test_async_context_age_and_http_endpoint_helpers() -> None:
    assert captured_at_is_recent(datetime.now(UTC).isoformat(), 300)
    assert not captured_at_is_recent("invalid", 300)
    assert not captured_at_is_recent(None, 300)
    assert not captured_at_is_recent(
        (datetime.now(UTC) + timedelta(seconds=10)).isoformat(), 300
    )
    assert _http_signal_endpoint("https://collector", "traces") == (
        "https://collector/v1/traces"
    )
    assert (
        _http_signal_endpoint("https://collector/v1/traces", "traces")
        == "https://collector/v1/traces"
    )


def test_otlp_exporter_builders_apply_protocol_endpoint_and_timeout(
    monkeypatch,
) -> None:
    from opentelemetry.exporter.otlp.proto.grpc import (
        metric_exporter as grpc_metric,
    )
    from opentelemetry.exporter.otlp.proto.grpc import (
        trace_exporter as grpc_trace,
    )
    from opentelemetry.exporter.otlp.proto.http import (
        metric_exporter as http_metric,
    )
    from opentelemetry.exporter.otlp.proto.http import (
        trace_exporter as http_trace,
    )

    created: list[tuple[str, dict]] = []

    def constructor(name):
        return lambda **kwargs: created.append((name, kwargs)) or name

    monkeypatch.setattr(
        grpc_trace, "OTLPSpanExporter", constructor("grpc-span")
    )
    monkeypatch.setattr(
        grpc_metric, "OTLPMetricExporter", constructor("grpc-metric")
    )
    monkeypatch.setattr(
        http_trace, "OTLPSpanExporter", constructor("http-span")
    )
    monkeypatch.setattr(
        http_metric, "OTLPMetricExporter", constructor("http-metric")
    )
    grpc = OTLPExporterConfig(
        endpoint="collector:4317",
        protocol="grpc",
        headers=(("x-test", "value"),),
        timeout_seconds=2,
    )
    assert _build_span_exporter(grpc) == "grpc-span"
    assert _build_metric_exporter(grpc) == "grpc-metric"
    http = OTLPExporterConfig(
        endpoint="https://collector/",
        protocol="http/protobuf",
        timeout_seconds=3,
    )
    assert _build_span_exporter(http) == "http-span"
    assert _build_metric_exporter(http) == "http-metric"
    assert created[2][1]["endpoint"] == "https://collector/v1/traces"
    assert created[3][1]["endpoint"] == "https://collector/v1/metrics"
    assert created[0][1]["headers"] == {"x-test": "value"}
    assert created[0][1]["timeout"] == 2


def test_google_exporter_kwargs_select_protocol_credentials(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "policyengine_observability.google_auth._google_grpc_credentials",
        lambda audience: f"grpc:{audience}",
    )
    monkeypatch.setattr(
        "policyengine_observability.google_auth._google_http_session",
        lambda audience, headers: (audience, dict(headers)),
    )
    grpc = _exporter_kwargs(
        OTLPExporterConfig(
            endpoint="collector:4317",
            auth=GoogleIdTokenAuth("https://collector"),
        )
    )
    assert grpc["credentials"] == "grpc:https://collector"
    http = _exporter_kwargs(
        OTLPExporterConfig(
            endpoint="https://collector",
            protocol="http/protobuf",
            auth=GoogleIdTokenAuth("https://collector"),
            headers=(("x", "y"),),
        )
    )
    assert http["session"] == ("https://collector", {"x": "y"})
    assert "headers" not in http


def test_google_id_token_uses_modal_workload_identity(monkeypatch) -> None:
    from google.auth import identity_pool, impersonated_credentials

    calls: dict[str, object] = {}

    class Source:
        @classmethod
        def from_info(cls, config, scopes):
            calls["source"] = (config, scopes)
            return "source-credentials"

    class Target:
        def __init__(self, **kwargs):
            calls["target"] = kwargs

    class Identity:
        def __init__(self, **kwargs):
            calls["identity"] = kwargs

    monkeypatch.setattr(identity_pool, "Credentials", Source)
    monkeypatch.setattr(impersonated_credentials, "Credentials", Target)
    monkeypatch.setattr(
        impersonated_credentials, "IDTokenCredentials", Identity
    )
    monkeypatch.setattr(
        "policyengine_observability.google_auth._write_subject_token",
        lambda token: f"/tmp/{token}",
    )
    monkeypatch.setenv("MODAL_IDENTITY_TOKEN", "modal-token")
    monkeypatch.setenv(
        "OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER",
        "projects/123/providers/example",
    )
    monkeypatch.setenv(
        "OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL",
        "modal@central.iam.gserviceaccount.com",
    )
    result = _google_id_token_credentials("https://collector")
    assert isinstance(result, Identity)
    assert calls["source"][0]["credential_source"]["file"] == (
        "/tmp/modal-token"
    )
    assert calls["identity"]["target_audience"] == "https://collector"


def test_google_id_token_falls_back_to_application_credentials(
    monkeypatch,
) -> None:
    from google.oauth2 import id_token

    monkeypatch.delenv("MODAL_IDENTITY_TOKEN", raising=False)
    monkeypatch.delenv("OBSERVABILITY_GOOGLE_OIDC_TOKEN", raising=False)
    monkeypatch.setattr(
        id_token,
        "fetch_id_token_credentials",
        lambda audience, request: (audience, request),
    )
    result = _google_id_token_credentials("https://collector")
    assert result[0] == "https://collector"


def test_google_transport_helpers(monkeypatch, tmp_path) -> None:
    import grpc
    from google.auth.transport import grpc as google_grpc
    from google.auth.transport import requests as google_requests

    monkeypatch.setattr(
        "policyengine_observability.google_auth._google_id_token_credentials",
        lambda audience: f"token:{audience}",
    )
    monkeypatch.setattr(
        google_grpc,
        "AuthMetadataPlugin",
        lambda credentials, request, default_host: (
            credentials,
            default_host,
        ),
    )
    monkeypatch.setattr(grpc, "ssl_channel_credentials", lambda: "ssl")
    monkeypatch.setattr(
        grpc, "metadata_call_credentials", lambda plugin: ("metadata", plugin)
    )
    monkeypatch.setattr(
        grpc,
        "composite_channel_credentials",
        lambda ssl, metadata: (ssl, metadata),
    )
    assert _google_grpc_credentials("https://collector.example") == (
        "ssl",
        (
            "metadata",
            ("token:https://collector.example", "collector.example"),
        ),
    )

    class Session:
        def __init__(self, credentials):
            self.credentials = credentials
            self.headers = {}

    monkeypatch.setattr(google_requests, "AuthorizedSession", Session)
    session = _google_http_session("https://collector", {"x": "y"})
    assert session.credentials == "token:https://collector"
    assert session.headers == {"x": "y"}

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    path = _write_subject_token("short-lived-token")
    assert open(path).read() == "short-lived-token"
    assert _workload_identity_audience("projects/123/provider") == (
        "//iam.googleapis.com/projects/123/provider"
    )


def test_otel_runtime_contains_span_and_propagation_failures(
    monkeypatch,
) -> None:
    from opentelemetry import trace
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    diagnostics = Diagnostics()
    otel = OTelRuntime(
        make_config(otel=OTelConfig(enabled=False)), diagnostics
    )

    class BrokenTracer:
        def start_as_current_span(self, *_args, **_kwargs):
            raise RuntimeError("start failed")

    otel.tracer = BrokenTracer()
    assert otel.start_span("broken") is None

    monkeypatch.setattr(
        trace,
        "get_current_span",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("context failed")),
    )
    assert otel.current_correlation() == {}
    otel.set_span_attributes({"key": "value"})
    assert otel.remote_span_context({}) is None

    monkeypatch.setattr(
        TraceContextTextMapPropagator,
        "extract",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("malformed")
        ),
    )
    monkeypatch.setattr(
        TraceContextTextMapPropagator,
        "inject",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("unavailable")
        ),
    )
    assert otel.extract({"traceparent": "bad"}) is None
    otel.inject({})
    assert diagnostics.count("failure.otel.span_start") == 1
    assert diagnostics.count("failure.otel.context_extract") == 1
    assert diagnostics.count("failure.otel.context_inject") == 1


def test_otel_end_span_metrics_flush_and_shutdown_fail_open() -> None:
    diagnostics = Diagnostics()
    otel = OTelRuntime(
        make_config(otel=OTelConfig(enabled=False)), diagnostics
    )

    class Span:
        def __init__(self):
            self.recorded = None
            self.status = None

        def record_exception(self, error):
            self.recorded = error

        def set_status(self, status):
            self.status = status

    class Manager:
        def __init__(self):
            self.args = None

        def __exit__(self, *args):
            self.args = args

    span = Span()
    manager = Manager()
    error = ValueError("application")
    otel.end_span(SpanHandle(manager=manager, span=span), error)
    assert span.recorded is error
    assert manager.args[1] is error
    otel.end_span(None)

    class Instrument:
        def add(self, *_args, **_kwargs):
            raise RuntimeError("metric failed")

    otel._error_count = Instrument()
    otel.record_error({"outcome": "error"})
    otel.record_dropped("log")

    class Provider:
        def force_flush(self, **_kwargs):
            raise TimeoutError("flush")

        def shutdown(self, **_kwargs):
            raise TimeoutError("shutdown")

    provider = Provider()
    otel._tracer_provider = provider
    otel._meter_provider = provider
    otel._owns_tracer_provider = True
    otel._owns_meter_provider = True
    otel.force_flush(0.01)
    otel.shutdown(0.01)
    assert diagnostics.count("failure.otel.metric_record") == 1
    assert diagnostics.count("failure.otel.force_flush") == 2
    assert diagnostics.count("failure.otel.trace_shutdown") == 1
    assert diagnostics.count("failure.otel.metric_shutdown") == 1


def test_lazy_exporters_delegate_flush_and_shutdown_failures() -> None:
    diagnostics = Diagnostics()

    class SpanDelegate:
        def export(self, spans):
            return SpanExportResult.SUCCESS

        def force_flush(self, _timeout):
            raise TimeoutError("flush")

        def shutdown(self):
            raise RuntimeError("shutdown")

    span_exporter = _LazySpanExporter(lambda: SpanDelegate(), diagnostics)
    assert span_exporter.export([]) is SpanExportResult.SUCCESS
    assert not span_exporter.force_flush()
    span_exporter.shutdown()

    class MetricDelegate:
        def export(self, *_args, **_kwargs):
            from opentelemetry.sdk.metrics.export import MetricExportResult

            return MetricExportResult.SUCCESS

        def force_flush(self, _timeout):
            raise TimeoutError("flush")

        def shutdown(self, **_kwargs):
            raise RuntimeError("shutdown")

    metric_exporter = _lazy_metric_exporter_class()(
        lambda: MetricDelegate(), diagnostics
    )
    from opentelemetry.sdk.metrics.export import MetricExportResult

    assert metric_exporter.export(object()) is MetricExportResult.SUCCESS
    assert not metric_exporter.force_flush()
    metric_exporter.shutdown()
    assert diagnostics.count("failure.otel.span_flush") == 1
    assert diagnostics.count("failure.otel.span_exporter_shutdown") == 1
    assert diagnostics.count("failure.otel.metric_flush") == 1
    assert diagnostics.count("failure.otel.metric_exporter_shutdown") == 1


def test_queue_depth_observation_is_bounded_and_failure_is_local() -> None:
    diagnostics = Diagnostics()
    otel = OTelRuntime(
        make_config(otel=OTelConfig(enabled=False)),
        diagnostics,
        queue_depth=lambda: -4,
    )
    observation = otel._observe_queue_depth(None)[0]
    assert observation.value == 0
    otel._queue_depth_callback = lambda: (_ for _ in ()).throw(
        RuntimeError("queue unavailable")
    )
    assert otel._observe_queue_depth(None) == []
    assert diagnostics.count("failure.otel.queue_depth") == 1


def test_concurrent_requests_keep_trace_and_span_context_separate() -> None:
    runtime, exporter = _runtime_with_spans()

    async def request(request_id: str) -> None:
        runtime.begin_request(
            headers={"X-PolicyEngine-Request-Id": request_id},
            method="GET",
            route="/concurrent",
        )
        async with runtime.span(f"child.{request_id}"):
            await asyncio.sleep(0)
            runtime.event("inside")
        runtime.end_request(status_code=200)

    async def run() -> None:
        await asyncio.gather(request("one"), request("two"))

    asyncio.run(run())
    completion_records = [
        item
        for item in records(runtime._delivery._stdout)
        if item.get("event.name") == "request.completed"
    ]
    traces = {
        item["request.id"]: item["trace_id"] for item in completion_records
    }
    assert traces["one"] != traces["two"]
    child_spans = {
        span.name: span
        for span in exporter.get_finished_spans()
        if span.parent
    }
    for request_id in ("one", "two"):
        child = child_spans[f"child.{request_id}"]
        assert format(child.context.trace_id, "032x") == traces[request_id]
    runtime.shutdown()
