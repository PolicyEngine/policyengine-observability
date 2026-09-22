from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, Literal

from .base import DestinationBuildContext, RecordWriter

GOOGLE_TRACE_KEY = "logging.googleapis.com/trace"
GOOGLE_SPAN_ID_KEY = "logging.googleapis.com/spanId"
GOOGLE_TRACE_SAMPLED_KEY = "logging.googleapis.com/trace_sampled"


@dataclass(frozen=True, slots=True)
class GoogleCloudLogFormatter:
    """Adds Cloud Logging trace-correlation fields to a record copy."""

    project_id: str

    def __call__(self, record: dict[str, Any]) -> dict[str, Any]:
        trace_id = record.get("trace_id")
        span_id = record.get("span_id")
        if trace_id and self.project_id.strip():
            record[GOOGLE_TRACE_KEY] = (
                f"projects/{self.project_id}/traces/{trace_id}"
            )
            record[GOOGLE_TRACE_SAMPLED_KEY] = bool(
                record.get("trace_sampled", False)
            )
        if span_id:
            record[GOOGLE_SPAN_ID_KEY] = str(span_id)
        return record


@dataclass(frozen=True, slots=True)
class GoogleCloudLogDestination:
    """Writes records directly to the Google Cloud Logging API."""

    project_id: str
    log_name: str
    queue_capacity: int = 1_000
    batch_size: int = 100
    write_timeout_seconds: float = 5.0
    name: str = "google_cloud"
    delivery: Literal["queued"] = "queued"

    def build_writer(self, context: DestinationBuildContext) -> RecordWriter:
        del context
        if not self.project_id.strip() or not self.log_name.strip():
            raise ValueError(
                "Google Cloud project_id and log_name must be non-empty."
            )
        return _GoogleCloudWriter(self)

    def diagnostics(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.project_id.strip():
            errors.append("Google Cloud logging requires project_id.")
        if not self.log_name.strip():
            errors.append("Google Cloud logging requires log_name.")
        _timeout = self.write_timeout_seconds
        if (
            not isinstance(_timeout, (int, float))
            or isinstance(_timeout, bool)
            or not 0.1 <= _timeout <= 60
        ):
            errors.append(
                "Google Cloud logging write_timeout_seconds must be between "
                "0.1 and 60."
            )
        return tuple(errors)


class _GoogleCloudWriter:
    """Lazy-importing Cloud Logging writer used only by a queue worker."""

    def __init__(self, config: GoogleCloudLogDestination) -> None:
        from google.cloud import logging_v2

        from ..google_credentials import load_google_credentials

        credentials = load_google_credentials(prefer_workload_identity=True)
        self._client = logging_v2.Client(
            project=config.project_id,
            credentials=credentials,
        )
        self._logger = self._client.logger(config.log_name)
        self._project_id = config.project_id
        self._timeout = max(
            0.1, min(float(config.write_timeout_seconds), 60.0)
        )
        self._bound_write_timeout()
        logging_v2._instrumentation_emitted = True

    def write(self, record: dict[str, Any]) -> None:
        self.write_many([record])

    def write_many(self, records: list[dict[str, Any]]) -> None:
        batch = self._logger.batch()
        for record in records:
            batch.log_struct(record, **self._entry_kwargs(record))
        batch.commit()

    def _entry_kwargs(self, record: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "severity": record.get("severity", "DEFAULT"),
        }
        trace_id = record.get("trace_id")
        span_id = record.get("span_id")
        if trace_id:
            kwargs["trace"] = f"projects/{self._project_id}/traces/{trace_id}"
        if span_id:
            kwargs["span_id"] = str(span_id)
        kwargs["trace_sampled"] = bool(record.get("trace_sampled", False))
        return kwargs

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _bound_write_timeout(self) -> None:
        api = getattr(self._client, "logging_api", None)
        gapic = getattr(api, "_gapic_api", None)
        if gapic is None:
            return
        from google.api_core import exceptions as api_exceptions
        from google.api_core.retry import Retry, if_exception_type

        retry = Retry(
            initial=0.1,
            maximum=1.0,
            multiplier=1.3,
            timeout=self._timeout,
            predicate=if_exception_type(
                api_exceptions.DeadlineExceeded,
                api_exceptions.InternalServerError,
                api_exceptions.ServiceUnavailable,
            ),
        )
        gapic.write_log_entries = functools.partial(
            gapic.write_log_entries,
            retry=retry,
            timeout=self._timeout,
        )
