from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..config import ObservabilityConfig
from .background import BackgroundEmitDestination
from .base import LogDestination
from .google_cloud_logging import GoogleCloudLoggingDestination
from .stdout import StdoutJsonDestination

# A destination that fails this many consecutive emits is disabled until a
# manager-level restart rebuilds destinations. In sync mode the emits are on
# the caller's (request) path; in async mode the same limit applies to the
# background emitter's consecutive batch failures, and a tripped emitter
# raises so this breaker fires through the same path. Either way, a
# persistently failing sink must not keep charging the host service.
DESTINATION_FAILURE_LIMIT = 3

_VALID_EMIT_MODES = {"sync", "async"}
_GOOGLE_DESTINATION_NAMES = {"google", "google_cloud", "google_cloud_logging"}

_Report = tuple[str, BaseException, dict[str, Any]]


class LogDestinationManager:
    def __init__(
        self,
        *,
        config: ObservabilityConfig,
        loggers: Mapping[str, logging.Logger],
        serializer: Callable[[dict[str, Any]], str],
        on_failure: Callable[..., None],
    ) -> None:
        self.config = config
        self.loggers = loggers
        self.serializer = serializer
        self.on_failure = on_failure
        self.destinations: list[LogDestination] = []
        self.configured = False
        self._consecutive_failures: dict[int, int] = {}
        # Serializes configure/restart/disable transitions. Reentrant so
        # a failure report fired while holding it can safely re-enter.
        self._lifecycle_lock = threading.RLock()

    def configure(self) -> None:
        with self._lifecycle_lock:
            reports = self._configure_locked()
        # Reports fire only after the new destination set is live, so a
        # report's own emission cannot re-enter configuration.
        self._fire(reports)

    def _configure_locked(self) -> list[_Report]:
        previous = list(self.destinations)
        reports: list[_Report] = []

        def deferred_on_failure(
            operation: str, exc: BaseException, **fields: Any
        ) -> None:
            reports.append((operation, exc, fields))

        destinations: list[LogDestination] = []
        for destination_name in self.config.log_destinations or ("stdout",):
            try:
                destinations.append(
                    self._build_destination(
                        destination_name, deferred_on_failure
                    )
                )
            except BaseException as exc:
                reports.append(
                    (
                        "logging.destination_config",
                        exc,
                        {"destination": destination_name},
                    )
                )
        if not destinations:
            destinations.append(self._stdout_destination())
            reports.append(
                (
                    "logging.destination_config",
                    RuntimeError(
                        "No configured observability log destination "
                        "initialized; falling back to stdout."
                    ),
                    {"destination": "stdout_fallback"},
                )
            )
        reports.extend(self._config_warnings())
        # The rebuilt objects get a fresh failure ledger; stale entries
        # would otherwise leak and can collide via id() reuse.
        self._consecutive_failures.clear()
        self.destinations = destinations
        self.configured = True
        # Close replaced destinations only after the new set is live, so
        # no emitter can reach a closed destination through the manager.
        for destination in previous:
            self._close_destination(destination, deferred_on_failure)
        return reports

    def _config_warnings(self) -> list[_Report]:
        warnings: list[_Report] = []
        emit_mode = (self.config.log_emit_mode or "").strip().lower()
        if emit_mode not in _VALID_EMIT_MODES:
            warnings.append(
                (
                    "logging.destination_config_warning",
                    ValueError(
                        "Unknown OBSERVABILITY_LOG_EMIT_MODE "
                        f"{self.config.log_emit_mode!r}; emission runs "
                        "synchronously."
                    ),
                    {},
                )
            )
        normalized_names = {
            name.strip().lower().replace("-", "_")
            for name in (self.config.log_destinations or ())
        }
        if (
            (self.config.stdout_format or "").strip().lower() == "google"
            and "stdout" in normalized_names
            and normalized_names & _GOOGLE_DESTINATION_NAMES
        ):
            warnings.append(
                (
                    "logging.destination_config_warning",
                    ValueError(
                        "stdout_format=google together with a "
                        "google_cloud_logging destination ingests every "
                        "record into Cloud Logging twice."
                    ),
                    {},
                )
            )
        return warnings

    def emit(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> None:
        emitted_payload = {**payload, "severity": severity}
        tripped: list[LogDestination] = []
        for destination in self._ensure_destinations():
            try:
                destination.emit(
                    emitted_payload,
                    log_type=log_type,
                    severity=severity,
                )
                self._consecutive_failures.pop(id(destination), None)
            except BaseException as exc:
                failures = (
                    self._consecutive_failures.get(id(destination), 0) + 1
                )
                self._consecutive_failures[id(destination)] = failures
                if failures >= DESTINATION_FAILURE_LIMIT:
                    tripped.append(destination)
                self.on_failure(
                    "logging.destination_emit",
                    exc,
                    destination=getattr(destination, "name", None),
                    log_type=log_type,
                    consecutive_failures=failures,
                )
        for destination in tripped:
            self._disable_destination(destination)

    def flush(self, deadline_seconds: float | None = None) -> None:
        """Flush all capable destinations against one shared deadline."""
        deadline: float | None = None
        if deadline_seconds is not None:
            deadline = time.monotonic() + max(0.0, deadline_seconds)
        for destination in list(self.destinations):
            flush = getattr(destination, "flush", None)
            if not callable(flush):
                continue
            remaining: float | None = None
            if deadline is not None:
                remaining = max(0.0, deadline - time.monotonic())
            try:
                flush(remaining)
            except Exception as exc:
                self.on_failure(
                    "logging.destination_flush",
                    exc,
                    destination=getattr(destination, "name", None),
                )

    def restart(self) -> None:
        """Rebuild destinations from config. Restart must revive
        destinations the breaker disabled and replace clients whose
        connections did not survive a fork or memory-snapshot restore,
        so it reconfigures from scratch rather than poking survivors."""
        with self._lifecycle_lock:
            self.configured = False
            try:
                reports = self._configure_locked()
            except BaseException as exc:
                self.destinations = [self._stdout_destination()]
                self.configured = True
                reports = [("logging.destination_restart", exc, {})]
        self._fire(reports)

    def _fire(self, reports: list[_Report]) -> None:
        for operation, exc, fields in reports:
            self.on_failure(operation, exc, **fields)

    def _close_destination(
        self,
        destination: LogDestination,
        report: Callable[..., None],
    ) -> None:
        close = getattr(destination, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception as exc:
            report(
                "logging.destination_close",
                exc,
                destination=getattr(destination, "name", None),
            )

    def _ensure_destinations(self) -> list[LogDestination]:
        if not self.configured:
            try:
                self.configure()
            except BaseException as exc:
                self.destinations = [self._stdout_destination()]
                self.configured = True
                self.on_failure("logging.destination_config", exc)
        return self.destinations

    def _disable_destination(self, destination: LogDestination) -> None:
        with self._lifecycle_lock:
            # Failure reporting can re-enter emit() and reach the limit
            # again before the outer call disables, and two threads can
            # race here; the membership check under the lock makes
            # disabling exactly-once.
            if destination not in self.destinations:
                return
            self.destinations = [
                existing
                for existing in self.destinations
                if existing is not destination
            ]
            self._consecutive_failures.pop(id(destination), None)
            if not self.destinations:
                self.destinations = [self._stdout_destination()]
        self._close_destination(destination, self.on_failure)
        self.on_failure(
            "logging.destination_disabled",
            RuntimeError(
                "Disabling observability log destination after "
                f"{DESTINATION_FAILURE_LIMIT} consecutive emit failures."
            ),
            destination=getattr(destination, "name", None),
        )

    def _build_destination(
        self,
        destination_name: str,
        report: Callable[..., None],
    ) -> LogDestination:
        normalized = destination_name.strip().lower().replace("-", "_")
        # Stdout is the fallback sink and stays synchronous; every other
        # destination goes through the async-wrapping choke point below.
        if normalized == "stdout":
            return self._stdout_destination()
        return self._maybe_background(
            self._build_network_destination(
                normalized, destination_name, report
            )
        )

    def _build_network_destination(
        self,
        normalized: str,
        destination_name: str,
        report: Callable[..., None],
    ) -> LogDestination:
        if normalized in _GOOGLE_DESTINATION_NAMES:
            return GoogleCloudLoggingDestination(
                project=self.config.google_cloud_project,
                log_name=self.config.google_cloud_log_name,
                timeout_seconds=self.config.google_log_timeout_seconds,
                on_failure=report,
            )
        raise ValueError(
            f"Unknown observability log destination: {destination_name}"
        )

    def _maybe_background(self, destination: LogDestination) -> LogDestination:
        emit_mode = (self.config.log_emit_mode or "").strip().lower()
        if emit_mode != "async":
            return destination
        return BackgroundEmitDestination(
            destination,
            on_failure=self.on_failure,
            queue_size=self.config.log_queue_size,
            batch_size=self.config.log_batch_size,
            batch_latency_seconds=self.config.log_batch_latency_seconds,
            flush_deadline_seconds=self.config.log_flush_deadline_seconds,
            failure_limit=DESTINATION_FAILURE_LIMIT,
        )

    def _stdout_destination(self) -> StdoutJsonDestination:
        return StdoutJsonDestination(
            loggers=self.loggers,
            serializer=self.serializer,
            output_format=self.config.stdout_format,
            google_cloud_project=self.config.google_cloud_project,
        )
