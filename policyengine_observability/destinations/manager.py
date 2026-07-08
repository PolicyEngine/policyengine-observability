from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..config import ObservabilityConfig
from .base import LogDestination
from .queued import QueuedLogDestination
from .registry import destination_strategy
from .stdout import StdoutJsonDestination, resolve_stdout_formatter

# A destination that fails this many consecutive emits is disabled for the
# rest of the process. Inline destinations emit synchronously on the
# caller's (request) path, so a persistently failing one — e.g. a broken
# serializer — must not keep charging every request; an observability sink
# can never be allowed to degrade the host service. Queued destinations
# never raise from emit (drops are counted internally), so this breaker
# only ever governs inline strategies.
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
        # Reconfigure (restart_observability) runs only from
        # single-threaded lifecycle moments by documented contract, so
        # replaced destinations can simply be closed before rebuilding.
        # The failure ledger is keyed by id(); stale entries could
        # otherwise attach to a new destination via id() reuse.
        previous = self.destinations
        self.destinations = []
        self._consecutive_failures.clear()
        self._close_destinations(previous)
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
        for warning in getattr(self.config, "config_warnings", ()):
            self.on_failure("logging.profile_config", ValueError(warning))

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

    def close(self, deadline_seconds: float | None = None) -> None:
        """Close every destination that supports closing, best-effort."""
        self._close_destinations(self.destinations, deadline_seconds)

    def _close_destinations(
        self,
        destinations: list[LogDestination],
        deadline_seconds: float | None = None,
    ) -> None:
        for destination in destinations:
            close = getattr(destination, "close", None)
            if not callable(close):
                continue
            try:
                close(deadline_seconds)
            except Exception as exc:
                self.on_failure(
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
        strategy = destination_strategy(destination_name)
        if strategy is None:
            raise ValueError(
                f"Unknown observability log destination: {destination_name}"
            )
        destination = strategy.factory(
            config=self.config,
            loggers=self.loggers,
            serializer=self.serializer,
        )
        if strategy.transport == "remote":
            # No remote strategy may ever write on a request thread.
            destination = QueuedLogDestination(
                inner=destination,
                on_failure=self.on_failure,
                maxsize=self.config.log_queue_maxsize,
                close_timeout_seconds=(
                    self.config.log_queue_close_timeout_seconds
                ),
            )
        return destination

    def _stdout_destination(self) -> StdoutJsonDestination:
        return StdoutJsonDestination(
            loggers=self.loggers,
            serializer=self.serializer,
            formatter=resolve_stdout_formatter(self.config),
        )
