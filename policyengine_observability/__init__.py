"""PolicyEngine observability version 2 public interface."""

from .adapters import instrument_fastapi, instrument_flask
from .config import (
    ConfigurationError,
    DeploymentIdentity,
    LoggingConfig,
    ObservabilityConfig,
    OTelConfig,
    OTLPAuthentication,
    OTLPExporterConfig,
    ServiceIdentity,
    TelemetryLimits,
)
from .destinations import (
    CustomLogDestination,
    GoogleCloudLogDestination,
    GoogleCloudLogFormatter,
    LogDestinationStrategy,
    RecordWriter,
    StdoutLogDestination,
)
from .google_auth import GoogleIdTokenAuth
from .integrations import instrument_httpx
from .runtime import (
    REQUEST_ID_HEADER,
    TRACEPARENT_HEADER,
    TRACESTATE_HEADER,
    ObservabilityLogHandler,
    ObservabilityRuntime,
    configure,
    instrument_logging,
)
from .schema import SCHEMA_VERSION

__all__ = [
    "REQUEST_ID_HEADER",
    "SCHEMA_VERSION",
    "TRACEPARENT_HEADER",
    "TRACESTATE_HEADER",
    "CustomLogDestination",
    "ConfigurationError",
    "DeploymentIdentity",
    "GoogleCloudLogDestination",
    "GoogleCloudLogFormatter",
    "GoogleIdTokenAuth",
    "LogDestinationStrategy",
    "LoggingConfig",
    "OTLPAuthentication",
    "OTLPExporterConfig",
    "OTelConfig",
    "ObservabilityConfig",
    "ObservabilityLogHandler",
    "ObservabilityRuntime",
    "RecordWriter",
    "ServiceIdentity",
    "TelemetryLimits",
    "StdoutLogDestination",
    "configure",
    "instrument_fastapi",
    "instrument_flask",
    "instrument_httpx",
    "instrument_logging",
]
