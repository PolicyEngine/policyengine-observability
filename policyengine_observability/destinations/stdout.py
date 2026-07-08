from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from .base import bounded_labels, normalize_payload, trace_resource_name

GOOGLE_TRACE_KEY = "logging.googleapis.com/trace"
GOOGLE_SPAN_ID_KEY = "logging.googleapis.com/spanId"
GOOGLE_LABELS_KEY = "logging.googleapis.com/labels"


class StdoutJsonDestination:
    """Writes structured payloads as JSON lines through stdlib loggers.

    In ``google`` output format the line carries the special keys the
    Cloud Run / GKE logging agent promotes to first-class LogEntry fields
    (severity, time, trace, span, labels), so agent-collected stdout gets
    full-fidelity ingestion with no in-process network emission.
    """

    name = "stdout"

    def __init__(
        self,
        *,
        loggers: Mapping[str, logging.Logger],
        serializer: Callable[[dict[str, Any]], str],
        output_format: str = "plain",
        google_cloud_project: str | None = None,
    ) -> None:
        self.loggers = loggers
        self.serializer = serializer
        self.output_format = output_format
        self.google_cloud_project = google_cloud_project

    def emit(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> None:
        logger = self.loggers.get(log_type) or self.loggers["event"]
        normalized = normalize_payload(payload)
        if self.output_format == "google":
            normalized = self._google_line(
                normalized,
                log_type=log_type,
                severity=severity,
            )
        message = self.serializer(normalized)
        if severity in {"ERROR", "CRITICAL"}:
            logger.error(message)
        elif severity == "WARNING":
            logger.warning(message)
        else:
            logger.info(message)

    def _google_line(
        self,
        normalized: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> dict[str, Any]:
        # normalize_payload returned a fresh dict; mutate it in place.
        # Stdout emission is synchronous, so the agent's receive time is
        # the event time and no explicit `time` key is needed.
        normalized["severity"] = str(severity).upper()
        trace = trace_resource_name(
            self.google_cloud_project,
            normalized.get("trace_id"),
        )
        if trace:
            normalized[GOOGLE_TRACE_KEY] = trace
        span_id = normalized.get("span_id")
        if span_id:
            normalized[GOOGLE_SPAN_ID_KEY] = str(span_id)
        normalized[GOOGLE_LABELS_KEY] = bounded_labels(
            normalized,
            log_type=log_type,
        )
        return normalized
