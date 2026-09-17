from __future__ import annotations

from runtime_helpers import (
    RecordingInstrument,
    RecordingPropagator,
    SegmentName,
    runtime,
)

from policyengine_observability import (
    ObservabilityConfig,
    RequestObservabilityContext,
)
from policyengine_observability.config import DEFAULT_METRIC_ATTRIBUTE_KEYS


def test_prepare_response_includes_traceparent_when_available() -> None:
    observed = runtime()
    observed.propagate = RecordingPropagator()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/trace",
        path="/trace",
        endpoint="trace",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    observed.begin_request(context)
    headers = observed.prepare_response(200)
    observed.teardown_request(None)

    assert headers["traceparent"].startswith("00-4bf92f")


def test_rate_limited_request_records_rate_limit_metric() -> None:
    observed = runtime()
    observed.rate_limited = RecordingInstrument()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/limited",
        path="/limited",
        endpoint="limited",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    observed.begin_request(context)
    headers = observed.finish_request(429)
    observed.teardown_request(None)

    assert headers["X-PolicyEngine-Request-Id"] == "request-1"
    assert context.attributes["rate_limited"] is True
    assert observed.rate_limited.calls[0][0] == "add"


def test_teardown_request_records_unhandled_exception() -> None:
    observed = runtime()
    observed.errors = RecordingInstrument()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/error",
        path="/error",
        endpoint="error",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    observed.begin_request(context)
    observed.teardown_request(RuntimeError("failed"))

    assert context.status_code == 500
    assert context.error is not None
    assert context.error.handled is False
    assert observed.errors.calls[0][0] == "add"


def test_from_env_invalid_shutdown_timeout_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS", "bad")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.shutdown_timeout_seconds == 3.0


def test_from_env_reads_stdout_format(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_STDOUT_FORMAT", "google")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.stdout_format == "google"


def test_from_env_reads_queue_knobs(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_LOG_QUEUE_MAXSIZE", "50")
    monkeypatch.setenv("OBSERVABILITY_LOG_QUEUE_CLOSE_TIMEOUT_SECONDS", "1.5")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.log_queue_maxsize == 50
    assert config.log_queue_close_timeout_seconds == 1.5


def test_from_env_queue_knobs_fall_back_on_garbage(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_LOG_QUEUE_MAXSIZE", "many")
    monkeypatch.setenv("OBSERVABILITY_LOG_QUEUE_CLOSE_TIMEOUT_SECONDS", "soon")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.log_queue_maxsize == 1000
    assert config.log_queue_close_timeout_seconds == 2.0


def test_from_env_enables_otel_by_default() -> None:
    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.otel_enabled is True


def test_from_env_allows_otel_opt_out(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_ENABLED", "false")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.otel_enabled is False


def test_from_env_ignores_legacy_observability_otel_switch(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OBSERVABILITY_OTEL_ENABLED", "false")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.otel_enabled is True


def test_from_env_reads_boolean_csv_and_environment(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_SERVICE_NAME", "env-svc")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "off")
    monkeypatch.setenv("OBSERVABILITY_REQUEST_LOGS_ENABLED", "false")
    monkeypatch.setenv("OBSERVABILITY_LOG_RAW_IP", "0")
    monkeypatch.setenv("OBSERVABILITY_LOG_LEVEL", "warning")
    monkeypatch.setenv("OTEL_ENABLED", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OBSERVABILITY_TRACER_NAME", "tracer")
    monkeypatch.setenv("OBSERVABILITY_METER_NAME", "meter")
    monkeypatch.setenv(
        "OBSERVABILITY_METRIC_ATTRIBUTE_KEYS",
        "service.name, custom",
    )
    monkeypatch.setenv(
        "OBSERVABILITY_EXTRA_METRIC_ATTRIBUTE_KEYS",
        "custom, other",
    )

    config = ObservabilityConfig.from_env(
        service_name="svc",
        instrument_fastapi=True,
        instrument_httpx=True,
    )

    assert config.service_name == "env-svc"
    assert config.environment == "production"
    assert config.enabled is False
    assert config.request_logs_enabled is False
    assert config.log_raw_ip is False
    assert config.otel_enabled is True
    assert config.otlp_endpoint == "http://collector"
    assert config.otlp_protocol == "http/protobuf"
    assert config.tracer_name == "tracer"
    assert config.meter_name == "meter"
    assert config.instrument_fastapi is True
    assert config.instrument_httpx is True
    assert config.metric_attribute_keys == ("service.name", "custom", "other")


def test_from_env_reads_log_destinations_and_google_config(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OBSERVABILITY_LOG_DESTINATIONS",
        "stdout, google-cloud-logging, stdout",
    )
    monkeypatch.setenv("GCP_PROJECT", "fallback-project")
    monkeypatch.setenv("OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME", "custom-log")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.log_destinations == ("stdout", "google-cloud-logging")
    assert config.google_cloud_project == "fallback-project"
    assert config.google_cloud_log_name == "custom-log"


def test_from_env_uses_default_log_destinations_without_env(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OBSERVABILITY_LOG_DESTINATIONS", raising=False)

    config = ObservabilityConfig.from_env(
        service_name="svc",
        default_log_destinations=("google_cloud_logging",),
    )

    assert config.log_destinations == ("google_cloud_logging",)


def test_from_env_log_destinations_env_overrides_default(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OBSERVABILITY_LOG_DESTINATIONS", "stdout")

    config = ObservabilityConfig.from_env(
        service_name="svc",
        default_log_destinations=("google_cloud_logging",),
    )

    assert config.log_destinations == ("stdout",)


def test_metric_attribute_keys_are_configurable() -> None:
    config = ObservabilityConfig(
        service_name="svc",
        metric_attribute_keys=("service.name", "tool"),
    )
    context = RequestObservabilityContext(
        config=config,
        request_id="request-1",
        method="POST",
        route="/chat",
        path="/chat",
        endpoint="chat",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )
    context.set_attribute("tool", "search")
    context.set_attribute("model", "claude")

    assert context.metric_attributes() == {
        "service.name": "svc",
        "tool": "search",
    }


def test_context_set_attribute_normalizes_enum_values() -> None:
    observed = runtime()
    operation = observed.start_operation("job")["operation"]
    request = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/",
        path="/",
        endpoint="root",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    operation.set_attribute("segment", SegmentName.LOAD)
    request.set_attribute("segment", SegmentName.SAVE)
    observed.end_operation({"operation": operation})

    assert operation.attributes["segment"] == "load"
    assert request.attributes["segment"] == "save"


def test_metric_attribute_keys_can_be_extended_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_EXTRA_METRIC_ATTRIBUTE_KEYS", "custom")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.metric_attribute_keys == (
        *DEFAULT_METRIC_ATTRIBUTE_KEYS,
        "custom",
    )


def test_segment_metric_uses_configured_metric_attribute_keys() -> None:
    observed = runtime(
        metric_attribute_keys=(
            "service.name",
            "route",
            "method",
            "segment",
            "tool",
        )
    )
    observed.segment_duration = RecordingInstrument()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="POST",
        route="/chat",
        path="/chat",
        endpoint="chat",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    observed.begin_request(context)
    with observed.segment(SegmentName.LOAD, tool="search", model="claude"):
        pass
    observed.finish_request(200)
    observed.teardown_request(None)

    _, _, attributes = observed.segment_duration.calls[0]
    assert attributes["tool"] == "search"
    assert "model" not in attributes


def test_shutdown_calls_trace_and_metric_providers() -> None:
    class Provider:
        def __init__(self) -> None:
            self.shutdown_called = False

        def shutdown(self) -> None:
            self.shutdown_called = True

    observed = runtime(shutdown_timeout_seconds=1)
    trace_provider = Provider()
    meter_provider = Provider()
    observed.tracer_provider = trace_provider
    observed.meter_provider = meter_provider

    observed.shutdown()

    assert trace_provider.shutdown_called
    assert meter_provider.shutdown_called
