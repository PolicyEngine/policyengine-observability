from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from .destinations import LogDestinationStrategy, StdoutLogDestination

Platform = Literal["google_cloud_run", "modal", "local", "other"]
OTLPProtocol = Literal["grpc", "http/protobuf"]
OTLPEndpointMode = Literal["base", "signal"]
ProviderMode = Literal["owned", "external"]


class ConfigurationError(ValueError):
    """Raised before startup when observability configuration is invalid."""

    def __init__(self, errors: tuple[str, ...]) -> None:
        self.errors = errors
        details = "\n".join(f"- {error}" for error in errors)
        super().__init__(f"Invalid observability configuration:\n{details}")


DEFAULT_APPLICATION_ATTRIBUTE_KEYS = frozenset(set())

DEFAULT_DISPATCH_ATTRIBUTE_KEYS = frozenset(set())

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
class LoggingConfig:
    destinations: tuple[LogDestinationStrategy, ...] = field(
        default_factory=lambda: (StdoutLogDestination(),)
    )
    capture_standard_library: bool = False
    replace_existing_handlers: bool = False
    minimum_severity: int = 20
    shutdown_timeout_seconds: float = 2.0


class OTLPAuthentication(Protocol):
    """Adds authentication-specific arguments to an OTLP exporter."""

    def exporter_kwargs(
        self,
        *,
        protocol: OTLPProtocol,
        headers: dict[str, str],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class OTLPExporterConfig:
    endpoint: str
    protocol: OTLPProtocol = "grpc"
    endpoint_mode: OTLPEndpointMode = "base"
    headers: tuple[tuple[str, str], ...] = ()
    auth: OTLPAuthentication | None = None
    timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class OTelConfig:
    enabled: bool = True
    traces: OTLPExporterConfig | None = None
    metrics: OTLPExporterConfig | None = None
    provider_mode: ProviderMode = "owned"
    sampling_ratio: float = 1.0
    span_queue_capacity: int = 2_048
    span_batch_size: int = 512
    span_schedule_delay_seconds: float = 5.0
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

        common_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        common_protocol = _protocol(
            "OTEL_EXPORTER_OTLP_PROTOCOL",
            os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"),
        )
        common_headers = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
        common_audience = os.getenv("POLICYENGINE_OTEL_GOOGLE_AUDIENCE")
        traces = _exporter_from_env(
            signal="traces",
            enabled=_export_enabled("OTEL_TRACES_EXPORTER"),
            common_endpoint=common_endpoint,
            common_protocol=common_protocol,
            common_headers=common_headers,
            common_audience=common_audience,
        )
        metrics = _exporter_from_env(
            signal="metrics",
            enabled=_export_enabled("OTEL_METRICS_EXPORTER"),
            common_endpoint=common_endpoint,
            common_protocol=common_protocol,
            common_headers=common_headers,
            common_audience=common_audience,
        )

        config = cls(
            service=service,
            deployment=deployment,
            logging=logging or LoggingConfig(),
            otel=OTelConfig(
                enabled=not _env_bool("OTEL_SDK_DISABLED", False),
                traces=traces,
                metrics=metrics,
                provider_mode=_provider_mode(
                    os.getenv("POLICYENGINE_OTEL_PROVIDER_MODE", "owned")
                ),
                sampling_ratio=_env_float(
                    "OTEL_TRACES_SAMPLER_ARG",
                    os.getenv("OTEL_TRACES_SAMPLER_ARG"),
                    default=1.0,
                    minimum=0.0,
                    maximum=1.0,
                ),
                span_queue_capacity=_env_int(
                    "OTEL_BSP_MAX_QUEUE_SIZE",
                    os.getenv("OTEL_BSP_MAX_QUEUE_SIZE"),
                    default=2_048,
                    minimum=1,
                    maximum=100_000,
                ),
                span_batch_size=_env_int(
                    "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
                    os.getenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE"),
                    default=512,
                    minimum=1,
                    maximum=10_000,
                ),
                span_schedule_delay_seconds=(
                    _env_float(
                        "OTEL_BSP_SCHEDULE_DELAY",
                        os.getenv("OTEL_BSP_SCHEDULE_DELAY"),
                        default=5_000.0,
                        minimum=1.0,
                        maximum=60_000.0,
                    )
                    / 1_000
                ),
                metric_export_interval_seconds=(
                    _env_float(
                        "OTEL_METRIC_EXPORT_INTERVAL",
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
        config.validate()
        return config

    def validate(self) -> None:
        """Reject invalid configuration before runtime setup has side effects."""

        errors = self.validation_errors()
        if errors:
            raise ConfigurationError(errors)

    def validation_errors(self) -> tuple[str, ...]:
        """Return invalid fields without creating runtime components."""

        errors: list[str] = []
        identity_values = {
            "service.name": self.service.name,
            "service.namespace": self.service.namespace,
            "service.version": self.service.version,
            "service.role": self.service.role,
            "deployment.environment": self.deployment.environment,
        }
        for key, value in identity_values.items():
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{key} must be a non-empty string.")

        if not isinstance(self.sensitive_values, tuple):
            errors.append(
                "sensitive_values must be a tuple of non-empty strings."
            )
        else:
            for index, value in enumerate(self.sensitive_values):
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"sensitive_values[{index}] must be a non-empty "
                        "string."
                    )

        for name, values in (
            ("application_attribute_keys", self.application_attribute_keys),
            ("dispatch_attribute_keys", self.dispatch_attribute_keys),
            ("metric_attribute_keys", self.metric_attribute_keys),
        ):
            if not isinstance(values, frozenset):
                errors.append(
                    f"{name} must be a frozenset of non-empty strings."
                )
                continue
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{name} entries must be non-empty strings.")

        _choice_error(
            errors,
            "deployment.platform",
            self.deployment.platform,
            {"google_cloud_run", "modal", "local", "other"},
        )
        if not isinstance(self.logging.minimum_severity, int) or isinstance(
            self.logging.minimum_severity, bool
        ):
            errors.append("logging.minimum_severity must be an integer.")
        _number_error(
            errors,
            "logging.shutdown_timeout_seconds",
            self.logging.shutdown_timeout_seconds,
            0,
            60,
        )

        for destination in self.logging.destinations:
            try:
                _choice_error(
                    errors,
                    f"logging destination {destination.name!r} delivery",
                    destination.delivery,
                    {"inline", "queued"},
                )
                _integer_error(
                    errors,
                    f"logging destination {destination.name!r} queue_capacity",
                    destination.queue_capacity,
                    1,
                    100_000,
                )
                _integer_error(
                    errors,
                    f"logging destination {destination.name!r} batch_size",
                    destination.batch_size,
                    1,
                    10_000,
                )
                validate = getattr(destination, "diagnostics", None)
                if callable(validate):
                    errors.extend(
                        str(item) for item in cast(tuple[str, ...], validate())
                    )
            except Exception as exc:
                errors.append(
                    "Invalid log destination strategy "
                    f"{getattr(destination, 'name', '<unnamed>')}: {exc}"
                )

        _choice_error(
            errors,
            "otel.provider_mode",
            self.otel.provider_mode,
            {"owned", "external"},
        )
        _number_error(
            errors, "otel.sampling_ratio", self.otel.sampling_ratio, 0, 1
        )
        _integer_error(
            errors,
            "otel.span_queue_capacity",
            self.otel.span_queue_capacity,
            1,
            100_000,
        )
        _integer_error(
            errors,
            "otel.span_batch_size",
            self.otel.span_batch_size,
            1,
            10_000,
        )
        _number_error(
            errors,
            "otel.span_schedule_delay_seconds",
            self.otel.span_schedule_delay_seconds,
            0.001,
            60,
        )
        _number_error(
            errors,
            "otel.metric_export_interval_seconds",
            self.otel.metric_export_interval_seconds,
            1,
            3_600,
        )
        _number_error(
            errors,
            "otel.shutdown_timeout_seconds",
            self.otel.shutdown_timeout_seconds,
            0,
            60,
        )
        for signal, exporter in (
            ("traces", self.otel.traces),
            ("metrics", self.otel.metrics),
        ):
            if exporter is None:
                continue
            if (
                not isinstance(exporter.endpoint, str)
                or not exporter.endpoint.strip()
            ):
                errors.append(
                    f"otel.{signal}.endpoint must be a non-empty string."
                )
            _choice_error(
                errors,
                f"otel.{signal}.protocol",
                exporter.protocol,
                {"grpc", "http/protobuf"},
            )
            _choice_error(
                errors,
                f"otel.{signal}.endpoint_mode",
                exporter.endpoint_mode,
                {"base", "signal"},
            )
            _number_error(
                errors,
                f"otel.{signal}.timeout_seconds",
                exporter.timeout_seconds,
                0.1,
                60,
            )

        for name, value in {
            "limits.max_attributes": self.limits.max_attributes,
            "limits.max_string_length": self.limits.max_string_length,
            "limits.max_error_message_length": self.limits.max_error_message_length,
            "limits.max_stack_length": self.limits.max_stack_length,
        }.items():
            _integer_error(errors, name, value, 1, 1_000_000)
        _number_error(
            errors,
            "limits.async_parent_max_age_seconds",
            self.limits.async_parent_max_age_seconds,
            0,
            86_400,
        )
        return tuple(errors)

    def diagnostics(self) -> tuple[str, ...]:
        messages: list[str] = []
        if (
            self.otel.enabled
            and self.otel.provider_mode == "owned"
            and self.otel.traces is None
            and self.otel.metrics is None
        ):
            messages.append(
                "Remote OTel export is disabled because no trace or metric "
                "OTLP endpoint is configured."
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
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(
        (f"{name} must be a boolean value; received {value!r}.",)
    )


def _provider_mode(value: str) -> ProviderMode:
    normalized = value.strip().lower()
    if normalized not in {"owned", "external"}:
        raise ConfigurationError(
            (f"POLICYENGINE_OTEL_PROVIDER_MODE is invalid: {value!r}.",)
        )
    return cast(ProviderMode, normalized)


def _protocol(name: str, value: str) -> OTLPProtocol:
    normalized = value.strip()
    if normalized not in {"grpc", "http/protobuf"}:
        raise ConfigurationError((f"{name} is invalid: {value!r}.",))
    return cast(OTLPProtocol, normalized)


def _export_enabled(name: str) -> bool:
    value = os.getenv(name, "otlp").strip().lower()
    if value not in {"otlp", "none"}:
        raise ConfigurationError((f"{name} is invalid: {value!r}.",))
    return value == "otlp"


def _exporter_from_env(
    *,
    signal: Literal["traces", "metrics"],
    enabled: bool,
    common_endpoint: str | None,
    common_protocol: OTLPProtocol,
    common_headers: str,
    common_audience: str | None,
) -> OTLPExporterConfig | None:
    if not enabled:
        return None
    prefix = f"OTEL_EXPORTER_OTLP_{signal.upper()}"
    signal_endpoint = os.getenv(f"{prefix}_ENDPOINT")
    endpoint = signal_endpoint or common_endpoint
    if not endpoint:
        return None
    protocol_value = os.getenv(f"{prefix}_PROTOCOL")
    protocol = (
        _protocol(f"{prefix}_PROTOCOL", protocol_value)
        if protocol_value is not None
        else common_protocol
    )
    headers_value = os.getenv(f"{prefix}_HEADERS")
    headers = _parse_headers(
        f"{prefix}_HEADERS"
        if headers_value is not None
        else "OTEL_EXPORTER_OTLP_HEADERS",
        headers_value if headers_value is not None else common_headers,
    )
    audience = (
        os.getenv(f"POLICYENGINE_OTEL_{signal.upper()}_GOOGLE_AUDIENCE")
        or common_audience
    )
    auth: OTLPAuthentication | None = None
    if audience:
        from .google_auth import GoogleIdTokenAuth

        auth = GoogleIdTokenAuth(audience)
    signal_timeout = os.getenv(f"{prefix}_TIMEOUT")
    timeout_name = (
        f"{prefix}_TIMEOUT"
        if signal_timeout is not None
        else "OTEL_EXPORTER_OTLP_TIMEOUT"
    )
    timeout = _env_float(
        timeout_name,
        signal_timeout or os.getenv("OTEL_EXPORTER_OTLP_TIMEOUT"),
        default=5_000.0,
        minimum=100.0,
        maximum=60_000.0,
    )
    return OTLPExporterConfig(
        endpoint=endpoint,
        protocol=protocol,
        endpoint_mode="signal" if signal_endpoint else "base",
        headers=headers,
        auth=auth,
        timeout_seconds=timeout / 1_000,
    )


def _parse_headers(name: str, value: str) -> tuple[tuple[str, str], ...]:
    headers: list[tuple[str, str]] = []
    for item in value.split(","):
        if not item.strip():
            continue
        if "=" not in item:
            raise ConfigurationError(
                (f"{name} contains a header without '=': {item!r}.",)
            )
        key, header_value = item.split("=", 1)
        if not key.strip():
            raise ConfigurationError(
                (f"{name} contains an empty header name.",)
            )
        headers.append((key.strip(), header_value.strip()))
    return tuple(headers)


def _env_float(
    name: str,
    value: str | None,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        raise ConfigurationError(
            (f"{name} must be a number; received {value!r}.",)
        ) from None
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ConfigurationError(
            (f"{name} must be between {minimum} and {maximum}.",)
        )
    return parsed


def _env_int(
    name: str,
    value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        raise ConfigurationError(
            (f"{name} must be an integer; received {value!r}.",)
        ) from None
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(
            (f"{name} must be between {minimum} and {maximum}.",)
        )
    return parsed


def _choice_error(
    errors: list[str], name: str, value: Any, choices: set[str]
) -> None:
    if not isinstance(value, str) or value not in choices:
        errors.append(f"{name} must be one of: {', '.join(sorted(choices))}.")


def _number_error(
    errors: list[str], name: str, value: Any, minimum: float, maximum: float
) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        errors.append(f"{name} must be between {minimum} and {maximum}.")


def _integer_error(
    errors: list[str], name: str, value: Any, minimum: int, maximum: int
) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        errors.append(f"{name} must be between {minimum} and {maximum}.")
