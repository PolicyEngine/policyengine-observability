from __future__ import annotations

import logging
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

    def configure(self) -> None:
        # Close destinations from a previous configure() so their worker
        # threads, clients, and atexit hooks are released, not orphaned.
        self._close_destinations(self.destinations)
        failures: list[tuple[str, BaseException]] = []
        destinations: list[LogDestination] = []
        for destination_name in self.config.log_destinations or ("stdout",):
            try:
                destinations.append(self._build_destination(destination_name))
            except BaseException as exc:
                failures.append((destination_name, exc))
        if not destinations:
            destinations.append(self._stdout_destination())
            failures.append(
                (
                    "stdout_fallback",
                    RuntimeError(
                        "No configured observability log destination "
                        "initialized; falling back to stdout."
                    ),
                )
            )
        self.destinations = destinations
        self.configured = True
        for destination_name, exc in failures:
            self.on_failure(
                "logging.destination_config",
                exc,
                destination=destination_name,
            )

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
        # Failure reporting can re-enter emit() and reach the limit again
        # before the outer call disables; make disabling idempotent.
        if destination not in self.destinations:
            return
        self.destinations = [
            existing
            for existing in self.destinations
            if existing is not destination
        ]
        self._consecutive_failures.pop(id(destination), None)
        self.on_failure(
            "logging.destination_disabled",
            RuntimeError(
                "Disabling observability log destination after "
                f"{DESTINATION_FAILURE_LIMIT} consecutive emit failures."
            ),
            destination=getattr(destination, "name", None),
        )
        if not self.destinations:
            self.destinations.append(self._stdout_destination())

    def _build_destination(self, destination_name: str) -> LogDestination:
        normalized = destination_name.strip().lower().replace("-", "_")
        # Stdout is the fallback sink and stays synchronous; every other
        # destination goes through the async-wrapping choke point below.
        if normalized == "stdout":
            return self._stdout_destination()
        return self._maybe_background(
            self._build_network_destination(normalized, destination_name)
        )

    def _build_network_destination(
        self,
        normalized: str,
        destination_name: str,
    ) -> LogDestination:
        if normalized in {"google", "google_cloud", "google_cloud_logging"}:
            return GoogleCloudLoggingDestination(
                project=self.config.google_cloud_project,
                log_name=self.config.google_cloud_log_name,
                timeout_seconds=self.config.google_log_timeout_seconds,
                on_failure=self.on_failure,
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

    def flush(self, deadline_seconds: float | None = None) -> None:
        for destination in list(self.destinations):
            flush = getattr(destination, "flush", None)
            if not callable(flush):
                continue
            try:
                flush(deadline_seconds)
            except BaseException as exc:
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
        self._consecutive_failures.clear()
        self.configured = False
        try:
            self.configure()
        except BaseException as exc:
            self.destinations = [self._stdout_destination()]
            self.configured = True
            self.on_failure("logging.destination_restart", exc)

    def _close_destinations(self, destinations: list[LogDestination]) -> None:
        for destination in destinations:
            close = getattr(destination, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except BaseException as exc:
                self.on_failure(
                    "logging.destination_close",
                    exc,
                    destination=getattr(destination, "name", None),
                )

    def _stdout_destination(self) -> StdoutJsonDestination:
        return StdoutJsonDestination(
            loggers=self.loggers,
            serializer=self.serializer,
            output_format=self.config.stdout_format,
            google_cloud_project=self.config.google_cloud_project,
        )
