from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..config import ObservabilityConfig
from .base import LogDestination
from .google_cloud_logging import GoogleCloudLoggingDestination
from .stdout import StdoutJsonDestination


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

    def configure(self) -> None:
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
        for destination in self._ensure_destinations():
            try:
                destination.emit(
                    emitted_payload,
                    log_type=log_type,
                    severity=severity,
                )
            except BaseException as exc:
                self.on_failure(
                    "logging.destination_emit",
                    exc,
                    destination=getattr(destination, "name", None),
                    log_type=log_type,
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

    def _build_destination(self, destination_name: str) -> LogDestination:
        normalized = destination_name.strip().lower().replace("-", "_")
        if normalized == "stdout":
            return self._stdout_destination()
        if normalized in {"google", "google_cloud", "google_cloud_logging"}:
            return GoogleCloudLoggingDestination(
                project=self.config.google_cloud_project,
                log_name=self.config.google_cloud_log_name,
            )
        raise ValueError(
            f"Unknown observability log destination: {destination_name}"
        )

    def _stdout_destination(self) -> StdoutJsonDestination:
        return StdoutJsonDestination(
            loggers=self.loggers,
            serializer=self.serializer,
        )
