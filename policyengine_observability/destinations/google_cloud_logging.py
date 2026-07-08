from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from policyengine_observability.google_credentials import (
    configure_google_application_credentials,
    load_google_credentials,
)

from .base import clamped, normalize_payload

DEFAULT_WRITE_TIMEOUT_SECONDS = 10.0
MIN_WRITE_TIMEOUT_SECONDS = 0.5
MAX_WRITE_TIMEOUT_SECONDS = 60.0


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
        write_timeout_seconds: float | None = None,
    ) -> None:
        self.project = project
        self.log_name = log_name
        self.write_timeout_seconds = clamped(
            write_timeout_seconds,
            low=MIN_WRITE_TIMEOUT_SECONDS,
            high=MAX_WRITE_TIMEOUT_SECONDS,
            default=DEFAULT_WRITE_TIMEOUT_SECONDS,
        )
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
        self._bound_write_timeout()

    def _bound_write_timeout(self) -> None:
        """Bound every write on this destination's own client.

        ``log_struct`` exposes no call options, and the transport default
        lets a degraded Logging API hold one write for up to 60 seconds.
        The gapic method is the single choke point underneath
        ``log_struct``, so rebind it on this client instance with a retry
        whose transient-error budget and per-call timeout are both capped
        by ``write_timeout_seconds``. When the private handle is absent
        (HTTP transport, injected fakes), the library default applies —
        acceptable because no write on this destination ever runs on a
        request thread.
        """
        api = getattr(self.client, "logging_api", None)
        gapic = getattr(api, "_gapic_api", None)
        if gapic is None:
            return
        try:
            from google.api_core import exceptions as api_exceptions
            from google.api_core.retry import Retry, if_exception_type
        except ImportError:  # pragma: no cover - google extra always has it
            return
        retry = Retry(
            initial=0.1,
            maximum=1.0,
            multiplier=1.3,
            timeout=self.write_timeout_seconds,
            predicate=if_exception_type(
                api_exceptions.DeadlineExceeded,
                api_exceptions.InternalServerError,
                api_exceptions.ServiceUnavailable,
            ),
        )
        gapic.write_log_entries = functools.partial(
            gapic.write_log_entries,
            retry=retry,
            timeout=self.write_timeout_seconds,
        )

    def emit(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
        timestamp: datetime | None = None,
    ) -> None:
        normalized = normalize_payload(payload)
        kwargs: dict[str, Any] = {
            "severity": severity,
            "labels": _labels(normalized, log_type=log_type),
        }
        if timestamp is not None:
            # Supplied by queued transports so delayed writes keep the
            # record's event time instead of the delivery time.
            kwargs["timestamp"] = timestamp
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
