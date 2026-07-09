from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..config import ObservabilityConfig
from .base import LogDestination, close_destination
from .queued import QueuedLogDestination
from .registry import destination_strategy
from .stdout import StdoutJsonDestination, build_stdout_destination

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
        # single-threaded lifecycle moments by documented contract.
        # Construction-time reports are deferred until the new
        # destinations are installed: reporting routes through emit, so
        # firing mid-build would recurse into configure.
        previous = self.destinations
        deferred: list[tuple[str, BaseException, dict[str, Any]]] = []

        def deferred_report(
            operation: str, exc: BaseException, **fields: Any
        ) -> None:
            deferred.append((operation, exc, fields))

        destinations: list[LogDestination] = []
        for destination_name in self.config.log_destinations or ("stdout",):
            try:
                destinations.append(
                    self._build_destination(
                        destination_name,
                        build_on_failure=deferred_report,
                    )
                )
            except BaseException as exc:
                deferred_report(
                    "logging.destination_config",
                    exc,
                    destination=destination_name,
                )
        if not destinations:
            destinations.append(self._stdout_destination())
            deferred_report(
                "logging.destination_config",
                RuntimeError(
                    "No configured observability log destination "
                    "initialized; falling back to stdout."
                ),
                destination="stdout_fallback",
            )
        self.destinations = destinations
        # The failure ledger is keyed by id(); clear it with the swap so
        # stale entries cannot attach to a new destination via id reuse.
        self._consecutive_failures.clear()
        self.configured = True
        # Close the replaced destinations only after the new ones are
        # installed, so their close-phase failure reports (which route
        # through emit) still have a sink.
        self._close_destinations(previous)
        for operation, exc, fields in deferred:
            self.on_failure(operation, exc, **fields)
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
        # One deadline covers the whole batch: each close gets whatever
        # budget the earlier ones left, so N stuck destinations cannot
        # take N times the budget. None means each destination applies
        # its own default (a reconfigure, not a bounded shutdown).
        deadline = (
            None
            if deadline_seconds is None
            else time.monotonic() + max(0.0, deadline_seconds)
        )
        for destination in destinations:
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
            close_destination(
                destination,
                on_failure=self.on_failure,
                deadline_seconds=remaining,
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

    def _build_destination(
        self,
        destination_name: str,
        *,
        build_on_failure: Callable[..., None],
    ) -> LogDestination:
        strategy = destination_strategy(destination_name)
        if strategy is None:
            raise ValueError(
                f"Unknown observability log destination: {destination_name}"
            )
        destination = strategy.factory(
            config=self.config,
            loggers=self.loggers,
            serializer=self.serializer,
            on_failure=build_on_failure,
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
        # The fail-open fallback: built directly (registry-free) and
        # with reporting suppressed, because this can run from inside a
        # failure-reporting path where another report would recurse.
        return build_stdout_destination(
            config=self.config,
            loggers=self.loggers,
            serializer=self.serializer,
        )
