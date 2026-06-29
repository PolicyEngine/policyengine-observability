from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from policyengine_observability.google_credentials import (
    configure_google_application_credentials,
    load_google_credentials,
)

from .base import normalize_payload


class GoogleCloudLogger(Protocol):
    def log_struct(
        self,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> None: ...


class GoogleCloudLoggingClient(Protocol):
    project: str | None

    def logger(self, log_name: str) -> GoogleCloudLogger: ...


GoogleCloudLoggingClientFactory = Callable[
    [str | None, object | None],
    GoogleCloudLoggingClient,
]


class GoogleCloudLoggingDestination:
    name = "google_cloud_logging"

    def __init__(
        self,
        *,
        project: str | None,
        log_name: str,
        client_factory: GoogleCloudLoggingClientFactory | None = None,
    ) -> None:
        self.project = project
        self.log_name = log_name
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
        normalized = normalize_payload(payload)
        kwargs: dict[str, Any] = {
            "severity": severity,
            "labels": _labels(normalized, log_type=log_type),
        }
        trace_id = normalized.get("trace_id")
        if trace_id and self.project:
            kwargs["trace"] = f"projects/{self.project}/traces/{trace_id}"
        span_id = normalized.get("span_id")
        if span_id:
            kwargs["span_id"] = span_id
        self.logger.log_struct(normalized, **kwargs)


def _labels(payload: dict[str, Any], *, log_type: str) -> dict[str, str]:
    labels = {"log_type": log_type}
    for key in (
        "service_name",
        "service_role",
        "environment",
        "schema_version",
    ):
        value = payload.get(key)
        if value is not None:
            labels[key] = str(value)
    return labels
