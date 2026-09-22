from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, cast

Platform = Literal["google_cloud_run", "modal", "local", "other"]
OTLPProtocol = Literal["grpc", "http/protobuf"]
ProviderMode = Literal["owned", "external"]

DEFAULT_APPLICATION_ATTRIBUTE_KEYS = frozenset(
    {
        "auth_result",
        "backend",
        "country_id",
        "job_type",
        "model_version",
        "requested_version",
        "resolved_channel",
        "simulation_year",
    }
)

DEFAULT_DISPATCH_ATTRIBUTE_KEYS = frozenset(
    {
        "job_id",
        "run_id",
        "simulation_id",
    }
)

DEFAULT_METRIC_ATTRIBUTE_KEYS = frozenset(
    {
        "service.name",
        "service.role",
        "deployment.environment.name",
        "cloud.platform",
        "http.route",
        "http.request.method",
        "http.response.status_code_class",
        "operation.name",
        "operation.kind",
        "outcome",
    }
)


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    name: str
    namespace: str
    version: str
    role: str


@dataclass(frozen=True, slots=True)
class DeploymentIdentity:
    environment: str
    platform: Platform
    region: str | None = None
    instance_id: str | None = None


@dataclass(frozen=True, slots=True)
class GoogleCloudLoggingConfig:
    project_id: str
    log_name: str = "policyengine-api-v1-modal"
    queue_capacity: int = 1_000
    batch_size: int = 1
    write_timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    stdout_enabled: bool = True
    remote: GoogleCloudLoggingConfig | None = None
    capture_standard_library: bool = False
    replace_existing_handlers: bool = False
    minimum_severity: int = 20
    shutdown_timeout_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class OTelConfig:
    enabled: bool = True
    traces_enabled: bool = True
    metrics_enabled: bool = True
    endpoint: str | None = None
    protocol: OTLPProtocol = "grpc"
    headers: tuple[tuple[str, str], ...] = ()
    google_audience: str | None = None
    provider_mode: ProviderMode = "owned"
    sampling_ratio: float = 1.0
    span_queue_capacity: int = 2_048
    span_batch_size: int = 512
    span_schedule_delay_seconds: float = 5.0
    export_timeout_seconds: float = 5.0
    metric_export_interval_seconds: float = 60.0
    shutdown_timeout_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class TelemetryLimits:
    max_attributes: int = 32
    max_string_length: int = 1_024
    max_error_message_length: int = 2_048
    max_stack_length: int = 16_384
    async_parent_max_age_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    service: ServiceIdentity
    deployment: DeploymentIdentity
    google_cloud_project_id: str | None = None
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    otel: OTelConfig = field(default_factory=OTelConfig)
    limits: TelemetryLimits = field(default_factory=TelemetryLimits)
    application_attribute_keys: frozenset[str] = (
        DEFAULT_APPLICATION_ATTRIBUTE_KEYS
    )
    dispatch_attribute_keys: frozenset[str] = DEFAULT_DISPATCH_ATTRIBUTE_KEYS
    metric_attribute_keys: frozenset[str] = DEFAULT_METRIC_ATTRIBUTE_KEYS
    sensitive_values: tuple[str, ...] = ()

    @classmethod
    def from_env(
        cls,
        *,
        service: ServiceIdentity,
        deployment: DeploymentIdentity,
        logging: LoggingConfig | None = None,
        google_cloud_project_id: str | None = None,
        limits: TelemetryLimits | None = None,
        application_attribute_keys: frozenset[str] | None = None,
        dispatch_attribute_keys: frozenset[str] | None = None,
        metric_attribute_keys: frozenset[str] | None = None,
        sensitive_values: tuple[str, ...] = (),
    ) -> ObservabilityConfig:
        """Read standard OTel transport settings with explicit identity.

        Service identity, deployment identity, and log routing are never
        inferred from ambient platform or Google Cloud variables.
        """

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").strip()
        if protocol not in {"grpc", "http/protobuf"}:
            protocol = "grpc"

        return cls(
            service=service,
            deployment=deployment,
            google_cloud_project_id=google_cloud_project_id,
            logging=logging or LoggingConfig(),
            otel=OTelConfig(
                enabled=not _env_bool("OTEL_SDK_DISABLED", False),
                traces_enabled=os.getenv("OTEL_TRACES_EXPORTER", "otlp")
                != "none",
                metrics_enabled=os.getenv("OTEL_METRICS_EXPORTER", "otlp")
                != "none",
                endpoint=endpoint,
                protocol=cast(OTLPProtocol, protocol),
                headers=_parse_headers(
                    os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
                ),
                google_audience=os.getenv("POLICYENGINE_OTEL_GOOGLE_AUDIENCE"),
                provider_mode=_provider_mode(
                    os.getenv("POLICYENGINE_OTEL_PROVIDER_MODE", "owned")
                ),
                sampling_ratio=_bounded_float(
                    os.getenv("OTEL_TRACES_SAMPLER_ARG"),
                    default=1.0,
                    minimum=0.0,
                    maximum=1.0,
                ),
                span_queue_capacity=_bounded_int(
                    os.getenv("OTEL_BSP_MAX_QUEUE_SIZE"),
                    default=2_048,
                    minimum=1,
                    maximum=100_000,
                ),
                span_batch_size=_bounded_int(
                    os.getenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE"),
                    default=512,
                    minimum=1,
                    maximum=10_000,
                ),
                span_schedule_delay_seconds=(
                    _bounded_float(
                        os.getenv("OTEL_BSP_SCHEDULE_DELAY"),
                        default=5_000.0,
                        minimum=1.0,
                        maximum=60_000.0,
                    )
                    / 1_000
                ),
                export_timeout_seconds=(
                    _bounded_float(
                        os.getenv("OTEL_BSP_EXPORT_TIMEOUT"),
                        default=5_000.0,
                        minimum=1.0,
                        maximum=60_000.0,
                    )
                    / 1_000
                ),
                metric_export_interval_seconds=(
                    _bounded_float(
                        os.getenv("OTEL_METRIC_EXPORT_INTERVAL"),
                        default=60_000.0,
                        minimum=1_000.0,
                        maximum=3_600_000.0,
                    )
                    / 1_000
                ),
            ),
            limits=limits or TelemetryLimits(),
            application_attribute_keys=(
                application_attribute_keys
                if application_attribute_keys is not None
                else DEFAULT_APPLICATION_ATTRIBUTE_KEYS
            ),
            dispatch_attribute_keys=(
                dispatch_attribute_keys
                if dispatch_attribute_keys is not None
                else DEFAULT_DISPATCH_ATTRIBUTE_KEYS
            ),
            metric_attribute_keys=(
                metric_attribute_keys
                if metric_attribute_keys is not None
                else DEFAULT_METRIC_ATTRIBUTE_KEYS
            ),
            sensitive_values=sensitive_values,
        )

    def diagnostics(self) -> tuple[str, ...]:
        messages: list[str] = []
        identity_values = {
            "service.name": self.service.name,
            "service.namespace": self.service.namespace,
            "service.version": self.service.version,
            "service.role": self.service.role,
            "deployment.environment.name": self.deployment.environment,
            "cloud.platform": self.deployment.platform,
        }
        for key, value in identity_values.items():
            if not str(value).strip():
                messages.append(f"Missing explicit runtime identity: {key}")

        remote = self.logging.remote
        if remote is not None and (
            not remote.project_id.strip() or not remote.log_name.strip()
        ):
            messages.append(
                "Remote logging is disabled because its project or log name "
                "is empty."
            )
        if remote is not None and self.deployment.platform != "modal":
            messages.append(
                "Direct Cloud Logging is disabled outside Modal; use platform "
                "standard-output ingestion."
            )
        if self.otel.enabled and not self.otel.endpoint:
            messages.append(
                "Remote OTel export is disabled because no OTLP endpoint is "
                "configured."
            )
        return tuple(messages)

    @property
    def identity_complete(self) -> bool:
        return not any(
            not str(value).strip()
            for value in (
                self.service.name,
                self.service.namespace,
                self.service.version,
                self.service.role,
                self.deployment.environment,
                self.deployment.platform,
            )
        )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _provider_mode(value: str) -> ProviderMode:
    return "external" if value.strip().lower() == "external" else "owned"


def _parse_headers(value: str) -> tuple[tuple[str, str], ...]:
    headers: list[tuple[str, str]] = []
    for item in value.split(","):
        if not item.strip() or "=" not in item:
            continue
        key, header_value = item.split("=", 1)
        if key.strip():
            headers.append((key.strip(), header_value.strip()))
    return tuple(headers)


def _bounded_float(
    value: str | None,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return min(max(parsed, minimum), maximum)


def _bounded_int(
    value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)
