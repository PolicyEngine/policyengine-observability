"""Shared destination fakes and manager builders for the test suite."""

from __future__ import annotations

import json
import logging
import threading

from policyengine_observability.config import ObservabilityConfig
from policyengine_observability.destinations.manager import (
    LogDestinationManager,
)


class RecordingLogger:
    """Captures level-routed serialized lines like a stdlib logger."""

    def __init__(self) -> None:
        self.lines = []

    def info(self, message) -> None:
        self.lines.append(("INFO", message))

    def warning(self, message) -> None:
        self.lines.append(("WARNING", message))

    def error(self, message) -> None:
        self.lines.append(("ERROR", message))


class RecordingDestination:
    name = "recording"

    def __init__(self) -> None:
        self.calls = []

    @property
    def payloads(self):
        return [payload for payload, *_ in self.calls]

    def emit(self, payload, *, log_type, severity, timestamp=None) -> None:
        self.calls.append((payload, log_type, severity, timestamp))


class ClosableRecordingDestination(RecordingDestination):
    """A recording destination with a zero-argument duck-typed close."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class TimestampBlindDestination:
    name = "timestamp-blind"

    def __init__(self) -> None:
        self.calls = []

    def emit(self, payload, *, log_type, severity) -> None:
        self.calls.append((payload, log_type, severity))


class BlockingDestination:
    """Blocks inside emit until released; sequenced via Events."""

    name = "blocking"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.closed = 0

    def emit(self, payload, *, log_type, severity, timestamp=None) -> None:
        self.calls += 1
        self.entered.set()
        self.release.wait(10)

    def close(self) -> None:
        self.closed += 1


class FailingDestination:
    """Raises from emit — always, or only for the first ``fail_first``."""

    name = "failing"

    def __init__(self, fail_first: int | None = None) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.delivered = []

    def emit(self, payload, *, log_type, severity, timestamp=None) -> None:
        self.calls += 1
        if self.fail_first is None or self.calls <= self.fail_first:
            raise RuntimeError("emit failed")
        self.delivered.append(payload)


def make_manager(config=None, *, loggers=None, destinations=None):
    """A manager with a failure-capturing on_failure.

    Returns ``(manager, failures)`` where each failure is the
    ``(operation, exception, fields)`` triple the manager reported.
    """
    failures = []
    manager = LogDestinationManager(
        config=config or ObservabilityConfig(),
        loggers=loggers or {"event": logging.getLogger("test-manager")},
        serializer=json.dumps,
        on_failure=lambda operation, exc, **fields: failures.append(
            (operation, exc, fields)
        ),
    )
    if destinations is not None:
        manager.destinations = list(destinations)
        manager.configured = True
    return manager, failures
