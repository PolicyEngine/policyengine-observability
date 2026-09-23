"""Logging destination strategies supplied to :class:`LoggingConfig`."""

from .base import (
    CustomLogDestination,
    DestinationBuildContext,
    LogDestinationStrategy,
    RecordFormatter,
    RecordWriter,
)
from .google_cloud import (
    GoogleCloudLogDestination,
    GoogleCloudLogFormatter,
)
from .stdout import StdoutLogDestination

__all__ = [
    "CustomLogDestination",
    "DestinationBuildContext",
    "GoogleCloudLogDestination",
    "GoogleCloudLogFormatter",
    "LogDestinationStrategy",
    "RecordFormatter",
    "RecordWriter",
    "StdoutLogDestination",
]
