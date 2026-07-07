from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from policyengine_observability.google_credentials import (
    configure_google_application_credentials,
    load_google_credentials,
)

from .base import bounded_labels, normalize_payload

DEFAULT_WRITE_TIMEOUT_SECONDS = 2.0


class GoogleCloudLogger(Protocol):
    full_name: str
    default_resource: Any


class GoogleCloudLoggingClient(Protocol):
    project: str | None

    def logger(self, log_name: str) -> GoogleCloudLogger: ...


GoogleCloudLoggingClientFactory = Callable[
    [str | None, object | None],
    GoogleCloudLoggingClient,
]


class GoogleCloudLoggingDestination:
    """Writes structured payloads to Google Cloud Logging.

    Writes go through the transport layer directly with an explicit
    per-call timeout and retries disabled: ``Logger.log_struct`` offers no
    call options, and the underlying defaults (60s retry deadline) would
    let a degraded Logging API stall the caller far longer than any
    observability write is worth. The ``Logger`` object is still used for
    its ``full_name`` and platform-detected ``default_resource``.
    """

    name = "google_cloud_logging"

    def __init__(
        self,
        *,
        project: str | None,
        log_name: str,
        client_factory: GoogleCloudLoggingClientFactory | None = None,
        timeout_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS,
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
            "logName": self.logger.full_name,
            "resource": _resource_dict(self.logger.default_resource),
            "jsonPayload": normalized,
            "severity": str(severity).upper(),
            "labels": bounded_labels(normalized, log_type=log_type),
        }
        trace_id = normalized.get("trace_id")
        if trace_id and self.project:
            entry["trace"] = f"projects/{self.project}/traces/{trace_id}"
        span_id = normalized.get("span_id")
        if span_id:
            entry["spanId"] = str(span_id)
        return entry

    def _write(self, entries: list[dict[str, Any]]) -> None:
        api = self.client.logging_api
        gapic_api = getattr(api, "_gapic_api", None)
        if gapic_api is not None:
            try:
                from google.cloud.logging_v2._gapic import (
                    _log_entry_mapping_to_pb,
                )
                from google.cloud.logging_v2.types import (
                    WriteLogEntriesRequest,
                )
            except ImportError:  # pragma: no cover - exotic installs only
                gapic_api = None
            else:
                request = WriteLogEntriesRequest(
                    entries=[
                        _log_entry_mapping_to_pb(entry) for entry in entries
                    ],
                    partial_success=True,
                )
                gapic_api.write_log_entries(
                    request=request,
                    retry=None,
                    timeout=self.timeout_seconds,
                )
                return
        # Non-gapic transports expose only the unbounded call; keep the
        # destination functional there rather than dropping logs.
        api.write_entries(entries, partial_success=True)


def _resource_dict(resource: Any) -> Any:
    to_dict = getattr(resource, "_to_dict", None)
    if callable(to_dict):
        return to_dict()
    return resource
