from __future__ import annotations

from .base import LogDestination, normalize_payload
from .google_cloud_logging import GoogleCloudLoggingDestination
from .manager import LogDestinationManager
from .stdout import StdoutJsonDestination

__all__ = [
    "GoogleCloudLoggingDestination",
    "LogDestination",
    "LogDestinationManager",
    "StdoutJsonDestination",
    "normalize_payload",
]
