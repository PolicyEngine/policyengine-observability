from __future__ import annotations

from .base import LogDestination, normalize_payload
from .google_cloud_logging import GoogleCloudLoggingDestination
from .manager import LogDestinationManager
from .queued import QueuedLogDestination
from .registry import register_destination
from .stdout import StdoutJsonDestination

__all__ = [
    "GoogleCloudLoggingDestination",
    "LogDestination",
    "LogDestinationManager",
    "QueuedLogDestination",
    "StdoutJsonDestination",
    "normalize_payload",
    "register_destination",
]
