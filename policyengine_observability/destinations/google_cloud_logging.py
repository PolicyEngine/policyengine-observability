from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from policyengine_observability.config import (
    DEFAULT_GOOGLE_LOG_TIMEOUT_SECONDS,
)
from policyengine_observability.google_credentials import (
    configure_google_application_credentials,
    load_google_credentials,
)

from .base import (
    bounded_labels,
    normalize_payload,
    rfc3339_timestamp,
    trace_resource_name,
)


class GoogleCloudLogger(Protocol):
    full_name: str
    default_resource: Any


class GoogleCloudLoggingClient(Protocol):
    project: str | None
    logging_api: Any

    def logger(self, log_name: str) -> GoogleCloudLogger: ...


GoogleCloudLoggingClientFactory = Callable[
    [str | None, object | None],
    GoogleCloudLoggingClient,
]


class GoogleCloudLoggingDestination:
    """Writes structured payloads to Google Cloud Logging.

    Writes go through the transport layer directly with a bounded retry
    and an explicit per-call timeout: ``Logger.log_struct`` offers no call
    options, and the underlying defaults (60s retry deadline) would let a
    degraded Logging API stall the caller far longer than any
    observability write is worth. The ``Logger`` object is still used for
    its ``full_name`` and platform-detected ``default_resource``. The
    bounded path needs the gapic transport; when it is unavailable the
    destination reports that once through ``on_failure`` and falls back
    to the unbounded ``write_entries`` call rather than dropping logs.
    """

    name = "google_cloud_logging"

    def __init__(
        self,
        *,
        project: str | None,
        log_name: str,
        client_factory: GoogleCloudLoggingClientFactory | None = None,
        timeout_seconds: float = DEFAULT_GOOGLE_LOG_TIMEOUT_SECONDS,
        on_failure: Callable[..., None] | None = None,
    ) -> None:
        self.project = project
        self.log_name = log_name
        self.timeout_seconds = timeout_seconds
        credentials = load_google_credentials(prefer_workload_identity=True)
        if credentials is None:
            configure_google_application_credentials()
        if client_factory is None:
            from google.cloud import logging as cloud_logging

            def client_factory(
                project_id: str | None,
                credentials: object | None,
            ) -> GoogleCloudLoggingClient:
                return cloud_logging.Client(
                    project=project_id,
                    credentials=credentials,
                )

        self.client = client_factory(project, credentials)
        self.project = project or getattr(self.client, "project", None)
        self.logger = self.client.logger(log_name)
        self._full_name = self.logger.full_name
        self._resource = _resource_dict(self.logger.default_resource)
        self._gapic_api = None
        self._gapic_tools: tuple[Any, Any] | None = None
        self._retry: Any = None
        self._resolve_transport(on_failure)

    def _resolve_transport(
        self, on_failure: Callable[..., None] | None
    ) -> None:
        api = getattr(self.client, "logging_api", None)
        gapic_api = getattr(api, "_gapic_api", None)
        if gapic_api is not None:
            try:
                from google.api_core import exceptions as api_exceptions
                from google.api_core.retry import (
                    Retry,
                    if_exception_type,
                )
                from google.cloud.logging_v2._gapic import (
                    _log_entry_mapping_to_pb,
                )
                from google.cloud.logging_v2.types import (
                    WriteLogEntriesRequest,
                )
            except ImportError:  # pragma: no cover - exotic installs only
                gapic_api = None
            else:
                self._gapic_api = gapic_api
                self._gapic_tools = (
                    _log_entry_mapping_to_pb,
                    WriteLogEntriesRequest,
                )
                # Retry transient errors, but only inside the overall
                # write budget, so a Logging API blip does not surface as
                # a destination failure while a real outage stays bounded.
                self._retry = Retry(
                    initial=0.1,
                    maximum=1.0,
                    multiplier=1.3,
                    timeout=self.timeout_seconds,
                    predicate=if_exception_type(
                        api_exceptions.DeadlineExceeded,
                        api_exceptions.InternalServerError,
                        api_exceptions.ServiceUnavailable,
                    ),
                )
        if self._gapic_api is None and on_failure is not None:
            on_failure(
                "logging.destination_unbounded_transport",
                RuntimeError(
                    "Google Cloud Logging gapic transport unavailable; "
                    "writes fall back to the unbounded write_entries "
                    "call."
                ),
                destination=self.name,
            )

    def emit(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> None:
        self._write(
            [self._build_entry(payload, log_type=log_type, severity=severity)]
        )

    def emit_batch(
        self,
        records: Sequence[tuple[dict[str, Any], str, str]],
    ) -> None:
        """Write ``(payload, log_type, severity)`` records in one call."""
        entries = [
            self._build_entry(payload, log_type=log_type, severity=severity)
            for payload, log_type, severity in records
        ]
        if entries:
            self._write(entries)

    def _build_entry(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> dict[str, Any]:
        normalized = normalize_payload(payload)
        entry: dict[str, Any] = {
            "logName": self._full_name,
            "resource": self._resource,
            "jsonPayload": normalized,
            "severity": str(severity).upper(),
            "labels": bounded_labels(normalized, log_type=log_type),
        }
        # Stamp the event time when the payload carries one; async
        # emission means the server's receive time can lag the event.
        timestamp = rfc3339_timestamp(normalized.get("created_at"))
        if timestamp:
            entry["timestamp"] = timestamp
        trace = trace_resource_name(self.project, normalized.get("trace_id"))
        if trace:
            entry["trace"] = trace
        span_id = normalized.get("span_id")
        if span_id:
            entry["spanId"] = str(span_id)
        return entry

    def _write(self, entries: list[dict[str, Any]]) -> None:
        if self._gapic_api is not None and self._gapic_tools is not None:
            mapping_to_pb, request_class = self._gapic_tools
            request = request_class(
                entries=[mapping_to_pb(entry) for entry in entries],
                partial_success=True,
            )
            self._gapic_api.write_log_entries(
                request=request,
                retry=self._retry,
                timeout=self.timeout_seconds,
            )
            return
        self.client.logging_api.write_entries(entries, partial_success=True)


def _resource_dict(resource: Any) -> Any:
    to_dict = getattr(resource, "_to_dict", None)
    if callable(to_dict):
        return to_dict()
    return resource
