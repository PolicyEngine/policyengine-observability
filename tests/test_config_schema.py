from __future__ import annotations

import json

import pytest
from conftest import make_config

from policyengine_observability import (
    SCHEMA_VERSION,
    ConfigurationError,
    CustomLogDestination,
    DeploymentIdentity,
    GoogleCloudLogDestination,
    GoogleCloudLogFormatter,
    LoggingConfig,
    ObservabilityConfig,
    OTelConfig,
    OTLPExporterConfig,
    ServiceIdentity,
    StdoutLogDestination,
    TelemetryLimits,
    configure,
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


def test_invalid_configuration_fails_before_destination_setup() -> None:
    destination_was_built = False

    def build_writer():
        nonlocal destination_was_built
        destination_was_built = True

    config = make_config(
        service=ServiceIdentity("", "", "", ""),
        deployment=DeploymentIdentity("", "invalid"),  # type: ignore[arg-type]
        logging=LoggingConfig(
            destinations=(
                GoogleCloudLogDestination(project_id="", log_name=""),
                CustomLogDestination(
                    name="unbuilt",
                    writer_factory=build_writer,
                    delivery="inline",
                ),
            ),
            minimum_severity="INVALID",  # type: ignore[arg-type]
        ),
    )

    with pytest.raises(ConfigurationError) as raised:
        configure(config)

    message = str(raised.value)
    assert "service.name must be a non-empty string" in message
    assert "deployment.platform" in message
    assert "logging.minimum_severity" in message
    assert "Google Cloud logging requires project_id" in message
    assert not destination_was_built


def test_missing_otel_exporters_are_a_nonfatal_diagnostic() -> None:
    config = make_config(otel=OTelConfig(enabled=True))

    config.validate()

    assert "no trace or metric OTLP endpoint" in " ".join(config.diagnostics())


def test_invalid_nested_limits_are_reported_together() -> None:
    exporter = OTLPExporterConfig(
        endpoint="",
        protocol="invalid",  # type: ignore[arg-type]
        timeout_seconds=0,
    )
    config = make_config(
        logging=LoggingConfig(shutdown_timeout_seconds=float("nan")),
        otel=OTelConfig(
            traces=exporter,
            provider_mode="invalid",  # type: ignore[arg-type]
            sampling_ratio=2,
            span_queue_capacity=0,
            span_batch_size=0,
            span_schedule_delay_seconds=0,
            metric_export_interval_seconds=0,
            shutdown_timeout_seconds=100,
        ),
        limits=TelemetryLimits(
            max_attributes=0,
            max_string_length=0,
            max_error_message_length=0,
            max_stack_length=0,
            async_parent_max_age_seconds=-1,
        ),
    )

    message = " ".join(config.validation_errors())

    for field in (
        "logging.shutdown_timeout_seconds",
        "otel.provider_mode",
        "otel.sampling_ratio",
        "otel.span_queue_capacity",
        "otel.span_batch_size",
        "otel.span_schedule_delay_seconds",
        "otel.metric_export_interval_seconds",
        "otel.shutdown_timeout_seconds",
        "otel.traces.endpoint",
        "otel.traces.protocol",
        "otel.traces.timeout_seconds",
        "limits.max_attributes",
        "limits.async_parent_max_age_seconds",
    ):
        assert field in message


def test_invalid_destination_strategies_are_reported() -> None:
    class InvalidDestination:
        name = "invalid"
        delivery = "network"
        queue_capacity = 0
        batch_size = 0

        def diagnostics(self):
            raise RuntimeError("validation failed")

    config = make_config(
        logging=LoggingConfig(
            destinations=(
                object(),  # type: ignore[arg-type]
                InvalidDestination(),  # type: ignore[arg-type]
                CustomLogDestination(
                    name="",
                    writer_factory=None,  # type: ignore[arg-type]
                    formatter=object(),  # type: ignore[arg-type]
                ),
                StdoutLogDestination(formatter=object()),  # type: ignore[arg-type]
                GoogleCloudLogDestination(
                    project_id="",
                    log_name="",
                    write_timeout_seconds=0,
                ),
            )
        )
    )

    message = " ".join(config.validation_errors())

    for expected in (
        "Invalid log destination strategy",
        "delivery must be one of",
        "queue_capacity",
        "batch_size",
        "validation failed",
        "name must be non-empty",
        "writer_factory must be callable",
        "formatter must be callable",
        "requires project_id",
        "requires log_name",
        "write_timeout_seconds",
    ):
        assert expected in message


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
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-one=1,x-two=2")
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


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OTEL_EXPORTER_OTLP_PROTOCOL", "invalid"),
        ("OTEL_TRACES_SAMPLER_ARG", "nan"),
        ("OTEL_BSP_MAX_QUEUE_SIZE", "bad"),
        ("POLICYENGINE_OTEL_PROVIDER_MODE", "invalid"),
        ("OTEL_TRACES_EXPORTER", "console"),
        ("OTEL_SDK_DISABLED", "sometimes"),
    ],
)
def test_from_env_rejects_invalid_values(monkeypatch, name, value) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=name):
        ObservabilityConfig.from_env(
            service=ServiceIdentity("svc", "ns", "1", "api"),
            deployment=DeploymentIdentity("dev", "local"),
        )


def test_from_env_rejects_malformed_headers(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "collector:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "missing-equals")

    with pytest.raises(ConfigurationError, match="without '='"):
        ObservabilityConfig.from_env(
            service=ServiceIdentity("svc", "ns", "1", "api"),
            deployment=DeploymentIdentity("dev", "local"),
        )


def test_from_env_accepts_false_boolean_and_disabled_exporters(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "no")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "collector:4317")

    config = ObservabilityConfig.from_env(
        service=ServiceIdentity("svc", "ns", "1", "api"),
        deployment=DeploymentIdentity("dev", "local"),
    )

    assert config.otel.enabled
    assert config.otel.traces is None
    assert config.otel.metrics is None


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
            "invalid",
            "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        ),
        ("OTEL_EXPORTER_OTLP_HEADERS", "=value", "empty header name"),
        ("OTEL_EXPORTER_OTLP_TIMEOUT", "50", "must be between"),
        ("OTEL_BSP_MAX_QUEUE_SIZE", "0", "must be between"),
    ],
)
def test_from_env_rejects_invalid_exporter_values(
    monkeypatch, name, value, message
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "collector:4317")
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=message):
        ObservabilityConfig.from_env(
            service=ServiceIdentity("svc", "ns", "1", "api"),
            deployment=DeploymentIdentity("dev", "local"),
        )


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
        application_attribute_keys=frozenset({"backend"}),
        sensitive_values=("secret-value",),
        limits=TelemetryLimits(
            max_string_length=64,
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
            attributes={"backend": "prefix-secret-value-suffix"},
            error=error,
        )
    assert "secret-value" not in str(record)
    assert "[REDACTED]" in str(record)
    assert record["attributes"]["backend"] == "prefix-[REDACTED]-suffix"
    assert len(record["error.stack"]) <= 100


def test_schema_redacts_before_truncating_strings() -> None:
    config = make_config(
        application_attribute_keys=frozenset({"backend"}),
        sensitive_values=("secret-value",),
        limits=TelemetryLimits(max_string_length=8),
    )

    record = build_record(
        config,
        severity="INFO",
        message="secret-value",
        attributes={"backend": "secret-value"},
    )

    assert record["message"] == "[REDACTE"
    assert record["attributes"]["backend"] == "[REDACTE"


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
