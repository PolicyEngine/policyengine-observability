from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from ..config import float_from_env
from .base import clamped, normalize_payload
from .google_credentials import (
    configure_google_application_credentials,
    load_google_credentials,
)
from .registry import register_destination
from .stdout import StdoutFormatter, register_stdout_formatter

DEFAULT_WRITE_TIMEOUT_SECONDS = 10.0
MIN_WRITE_TIMEOUT_SECONDS = 0.5
MAX_WRITE_TIMEOUT_SECONDS = 60.0

# Structured-JSON keys the Cloud Run/GKE logging agent promotes to
# first-class LogEntry fields when it ingests a stdout line.
GOOGLE_TRACE_KEY = "logging.googleapis.com/trace"
GOOGLE_SPAN_ID_KEY = "logging.googleapis.com/spanId"
GOOGLE_LABELS_KEY = "logging.googleapis.com/labels"


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
        self._suppress_instrumentation_entry()

    def _suppress_instrumentation_entry(self) -> None:
        """Keep the library's diagnostic entry out of the log stream.

        ``log_struct`` prepends a one-time instrumentation diagnostic
        entry to the first write per process; observability entries
        should be only the records we were asked to write.
        """
        try:
            from google.cloud import logging_v2

            logging_v2._instrumentation_emitted = True
        except ImportError:  # pragma: no cover - google extra has it
            pass

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
            kwargs["trace"] = _trace_resource(self.project, trace_id)
        span_id = normalized.get("span_id")
        if span_id:
            kwargs["span_id"] = span_id
        self.logger.log_struct(normalized, **kwargs)

    def close(self) -> None:
        """Release the owned client's transport, if it supports it."""
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


def _trace_resource(project: str, trace_id: str) -> str:
    # The LogEntry trace resource name; the direct write path and the
    # agent-native stdout formatter must build it identically for trace
    # correlation to work.
    return f"projects/{project}/traces/{trace_id}"


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


def _google_stdout_formatter_factory(config: Any) -> StdoutFormatter:
    """Shape stdout lines with the agent-native Cloud Logging keys.

    Emission stays synchronous, so no ``time`` key is set — the agent's
    receive time is the event time.
    """
    project = getattr(config, "google_cloud_project", None)

    def format_google(
        payload: dict[str, Any], *, log_type: str, severity: str
    ) -> dict[str, Any]:
        payload["severity"] = str(severity).upper()
        payload[GOOGLE_LABELS_KEY] = _labels(payload, log_type=log_type)
        trace_id = payload.get("trace_id")
        if trace_id and project:
            payload[GOOGLE_TRACE_KEY] = _trace_resource(project, trace_id)
        span_id = payload.get("span_id")
        if span_id:
            payload[GOOGLE_SPAN_ID_KEY] = str(span_id)
        return payload

    return format_google


register_stdout_formatter("google", _google_stdout_formatter_factory)


def _google_destination_factory(*, config: Any, **_: Any):
    return GoogleCloudLoggingDestination(
        project=config.google_cloud_project,
        log_name=config.google_cloud_log_name,
        # Backend knobs belong to the strategy: parsed here at
        # construction (so re-read on restart_observability()), keeping
        # per-backend fields off the core config dataclass.
        write_timeout_seconds=float_from_env(
            "OBSERVABILITY_GOOGLE_WRITE_TIMEOUT_SECONDS",
            DEFAULT_WRITE_TIMEOUT_SECONDS,
        ),
    )


register_destination(
    "google_cloud_logging",
    _google_destination_factory,
    transport="remote",
    aliases=("google", "google_cloud"),
    required_config=("google_cloud_project",),
)
