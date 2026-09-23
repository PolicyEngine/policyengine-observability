from __future__ import annotations

from policyengine_observability import (
    DeploymentIdentity,
    GoogleIdTokenAuth,
    ObservabilityConfig,
    OTLPExporterConfig,
    ServiceIdentity,
)


def test_signal_specific_otlp_environment_configuration(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://common")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://traces")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "https://metrics"
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "trace-key=trace-value"
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS", "metric-key=metric-value"
    )

    config = ObservabilityConfig.from_env(
        service=ServiceIdentity("service", "namespace", "1", "api"),
        deployment=DeploymentIdentity("test", "other"),
    )

    assert config.otel.traces == OTLPExporterConfig(
        endpoint="https://traces",
        endpoint_mode="signal",
        headers=(("trace-key", "trace-value"),),
    )
    assert config.otel.metrics == OTLPExporterConfig(
        endpoint="https://metrics",
        endpoint_mode="signal",
        headers=(("metric-key", "metric-value"),),
    )


def test_google_authentication_is_an_explicit_exporter_strategy(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "collector:4317")
    monkeypatch.setenv(
        "POLICYENGINE_OTEL_GOOGLE_AUDIENCE", "https://collector"
    )

    config = ObservabilityConfig.from_env(
        service=ServiceIdentity("service", "namespace", "1", "worker"),
        deployment=DeploymentIdentity("test", "modal"),
    )

    assert isinstance(config.otel.traces.auth, GoogleIdTokenAuth)
    assert isinstance(config.otel.metrics.auth, GoogleIdTokenAuth)
    assert config.otel.traces.auth.audience == "https://collector"
