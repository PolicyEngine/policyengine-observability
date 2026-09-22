from __future__ import annotations

import json

from conftest import make_config

from policyengine_observability import (
    SCHEMA_VERSION,
    DeploymentIdentity,
    GoogleCloudLogDestination,
    GoogleCloudLogFormatter,
    LoggingConfig,
    ObservabilityConfig,
    OTelConfig,
    OTLPExporterConfig,
    ServiceIdentity,
    TelemetryLimits,
)
from policyengine_observability.schema import (
    build_record,
    metric_attributes,
    normalize_attributes,
)


def test_explicit_identity_is_required_by_constructor() -> None:
    config = make_config()
    assert config.identity_complete
    assert config.service.namespace == "policyengine.test"
    assert config.deployment.platform == "local"


def test_incomplete_identity_reports_diagnostics() -> None:
    config = make_config(
        service=ServiceIdentity("", "", "", ""),
        deployment=DeploymentIdentity("", "other"),
        logging=LoggingConfig(
            destinations=(
                GoogleCloudLogDestination(project_id="", log_name=""),
            )
        ),
        otel=OTelConfig(enabled=True),
    )
    messages = " ".join(config.diagnostics())
    assert not config.identity_complete
    assert "Missing explicit runtime identity" in messages
    assert "Google Cloud logging is disabled" in messages
    assert "no trace or metric OTLP endpoint" in messages


def test_remote_logging_is_not_selected_from_platform() -> None:
    config = make_config(
        logging=LoggingConfig(
            destinations=(
                GoogleCloudLogDestination(
                    project_id="central", log_name="application"
                ),
            )
        )
    )
    assert config.diagnostics() == ()


def test_from_env_reads_transport_but_not_identity(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "ambient-project")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-one=1,bad,x-two=2")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "12")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "99")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "2500")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1500")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "4000")
    service = ServiceIdentity("svc", "ns", "1", "api")
    deployment = DeploymentIdentity("prod", "google_cloud_run")
    config = ObservabilityConfig.from_env(
        service=service, deployment=deployment
    )
    assert config.service is service
    assert config.deployment is deployment
    assert config.otel.traces == OTLPExporterConfig(
        endpoint="https://collector",
        protocol="http/protobuf",
        headers=(("x-one", "1"), ("x-two", "2")),
        timeout_seconds=1.5,
    )
    assert config.otel.metrics == config.otel.traces
    assert config.otel.sampling_ratio == 0.25
    assert config.otel.span_queue_capacity == 12
    assert config.otel.span_batch_size == 99
    assert config.otel.span_schedule_delay_seconds == 2.5
    assert config.otel.metric_export_interval_seconds == 4.0


def test_from_env_invalid_values_are_bounded(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "invalid")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "nan")
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "bad")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "yes")
    config = ObservabilityConfig.from_env(
        service=ServiceIdentity("svc", "ns", "1", "api"),
        deployment=DeploymentIdentity("dev", "local"),
    )
    assert config.otel.traces is None
    assert config.otel.metrics is None
    assert config.otel.sampling_ratio == 1.0
    assert config.otel.span_queue_capacity == 2_048
    assert not config.otel.enabled


def test_schema_preserves_core_fields_and_namespaces_attributes() -> None:
    config = make_config(
        application_attribute_keys=frozenset({"service.name", "backend"})
    )
    record = build_record(
        config,
        severity="info",
        event_name="work.started",
        context={"request.id": "request-1"},
        attributes={"service.name": "attacker", "backend": "modal"},
    )
    assert record["schema_version"] == SCHEMA_VERSION
    assert record["service.name"] == "test-api"
    assert record["attributes"]["service.name"] == "attacker"
    assert record["attributes"]["backend"] == "modal"
    json.dumps(record)


def test_schema_redacts_and_truncates_errors() -> None:
    config = make_config(
        sensitive_values=("secret-value",),
        limits=TelemetryLimits(
            max_string_length=8,
            max_error_message_length=64,
            max_stack_length=100,
        ),
    )
    try:
        raise ValueError("secret-value should disappear")
    except ValueError as error:
        record = build_record(
            config,
            severity="ERROR",
            message="secret-value message",
            error=error,
        )
    assert "secret-value" not in str(record)
    assert "[REDACTED]" in str(record)
    assert len(record["error.stack"]) <= 100


def test_attribute_policy_omits_sensitive_non_scalar_and_nonfinite() -> None:
    config = make_config(
        application_attribute_keys=frozenset(
            {"allowed", "authorization", "items", "infinite", "flag"}
        )
    )
    safe, omitted = normalize_attributes(
        {
            "allowed": "value",
            "authorization": "bearer",
            "items": [1, 2],
            "infinite": float("inf"),
            "flag": True,
        },
        config,
        allowed_keys=config.application_attribute_keys,
    )
    assert safe == {"allowed": "value", "flag": True}
    assert omitted == 3


def test_attribute_count_and_string_length_are_bounded() -> None:
    config = make_config(
        application_attribute_keys=frozenset({"one", "two", "three"}),
        limits=TelemetryLimits(max_attributes=2, max_string_length=3),
    )
    safe, omitted = normalize_attributes(
        {"one": "abcdef", "two": 2, "three": 3}, config
    )
    assert safe == {"one": "abc", "two": 2}
    assert omitted == 1


def test_google_trace_correlation_is_not_in_canonical_record() -> None:
    record = build_record(
        make_config(),
        severity="INFO",
        event_name="correlated",
        context={
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "trace_sampled": True,
        },
    )
    assert "logging.googleapis.com/trace" not in record
    assert "logging.googleapis.com/spanId" not in record
    assert "logging.googleapis.com/trace_sampled" not in record
    formatted = GoogleCloudLogFormatter("central-project")(record)
    assert formatted["logging.googleapis.com/trace"] == (
        "projects/central-project/traces/" + "a" * 32
    )
    assert formatted["logging.googleapis.com/spanId"] == "b" * 16
    assert formatted["logging.googleapis.com/trace_sampled"] is True


def test_metric_attributes_use_separate_allowlist() -> None:
    config = make_config()
    assert metric_attributes(
        {"service.name": "test-api", "job_id": "high-cardinality"}, config
    ) == {"service.name": "test-api"}
