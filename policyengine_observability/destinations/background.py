from __future__ import annotations

import atexit
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from ..config import (
    DEFAULT_LOG_BATCH_LATENCY_SECONDS,
    DEFAULT_LOG_BATCH_SIZE,
    DEFAULT_LOG_FLUSH_DEADLINE_SECONDS,
    DEFAULT_LOG_QUEUE_SIZE,
)
from .base import LogDestination, normalize_payload

DEFAULT_FAILURE_LIMIT = 3
DEFAULT_FAILURE_BACKOFF_SECONDS = 1.0
MIN_BATCH_LATENCY_SECONDS = 0.01
MAX_BATCH_SIZE = 500
DROP_REPORT_EVERY = 100

# _drain_once outcomes: each signal has exactly one meaning.
DRAIN_EMPTY = "empty"
DRAIN_DELIVERED = "delivered"
DRAIN_FAILED = "failed"

_LogRecord = tuple[dict[str, Any], str, str]


class BackgroundEmitDestination:
    """Decouples log acceptance from emission.

    ``emit()`` normalizes the payload (snapshotting it against caller
    mutation), appends it to a bounded in-memory buffer, and returns
    immediately — it never blocks on the network. When the buffer is
    full the oldest record is dropped (counted and reported, throttled),
    keeping the freshest records: during a sink outage the most
    diagnostic records are the recent ones. A daemon worker drains the
    buffer in batches to the wrapped destination (preferring its
    ``emit_batch``), backing off between failed batches; after
    ``failure_limit`` consecutive failed batches the wrapper trips, and
    subsequent ``emit()`` calls raise so the manager's circuit breaker
    disables it through the same path as a synchronous destination.

    The worker starts lazily on first emit and is pid-aware, so a forked
    process starts a fresh worker automatically; ``restart()``
    additionally clears buffered and trip state. Recovery of a tripped
    and disabled destination happens at the manager level
    (``LogDestinationManager.restart`` rebuilds destinations).
    """

    def __init__(
        self,
        wrapped: LogDestination,
        *,
        on_failure: Callable[..., None],
        queue_size: int = DEFAULT_LOG_QUEUE_SIZE,
        batch_size: int = DEFAULT_LOG_BATCH_SIZE,
        batch_latency_seconds: float = DEFAULT_LOG_BATCH_LATENCY_SECONDS,
        flush_deadline_seconds: float = DEFAULT_LOG_FLUSH_DEADLINE_SECONDS,
        failure_limit: int = DEFAULT_FAILURE_LIMIT,
        failure_backoff_seconds: float = DEFAULT_FAILURE_BACKOFF_SECONDS,
    ) -> None:
        self.wrapped = wrapped
        self.name = getattr(wrapped, "name", "background")
        self.on_failure = on_failure
        self.queue_size = max(1, queue_size)
        self.batch_size = min(max(1, batch_size), MAX_BATCH_SIZE)
        self.batch_latency_seconds = max(
            MIN_BATCH_LATENCY_SECONDS, batch_latency_seconds
        )
        self.flush_deadline_seconds = max(0.0, flush_deadline_seconds)
        self.failure_limit = failure_limit
        self.failure_backoff_seconds = max(0.0, failure_backoff_seconds)
        self._buffer: deque[_LogRecord] = deque(maxlen=self.queue_size)
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        # One wake event for the object's lifetime; only the closed event
        # is generation-scoped, so emitters can never signal a stale one.
        self._wake = threading.Event()
        self._tripped = threading.Event()
        self._closed = threading.Event()
        self._worker: threading.Thread | None = None
        self._pid: int | None = None
        self._consecutive_failures = 0
        self._dropped = 0
        self._in_flight = 0
        self._atexit_registered = False

    def emit(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> None:
        if self._tripped.is_set():
            raise RuntimeError(
                f"Background emitter for {self.name} is tripped after "
                f"{self.failure_limit} consecutive batch failures."
            )
        self._ensure_worker()
        # Snapshot now: the caller may keep mutating nested structures
        # after this returns, and serialization happens on the worker.
        record = (normalize_payload(payload), log_type, severity)
        dropped_total: int | None = None
        with self._lock:
            if len(self._buffer) == self.queue_size:
                self._dropped += 1
                if (
                    self._dropped == 1
                    or self._dropped % DROP_REPORT_EVERY == 0
                ):
                    dropped_total = self._dropped
            self._buffer.append(record)
        if dropped_total is not None:
            self.on_failure(
                "logging.destination_queue_overflow",
                RuntimeError(
                    "Background emitter buffer is full; dropping oldest "
                    "log records."
                ),
                destination=self.name,
                dropped_total=dropped_total,
            )
        self._wake.set()

    def flush(self, deadline_seconds: float | None = None) -> None:
        """Drain the buffer from the caller's thread, bounded by a
        deadline; waits for a worker-held in-flight batch and reports
        any undelivered remainder."""
        if deadline_seconds is None:
            deadline_seconds = self.flush_deadline_seconds
        deadline = time.monotonic() + deadline_seconds
        while not self._tripped.is_set() and time.monotonic() < deadline:
            outcome = self._drain_once()
            if outcome != DRAIN_EMPTY:
                continue
            with self._lock:
                idle = not self._buffer and self._in_flight == 0
            if idle:
                break
            # A batch is in flight on the worker; give it a moment.
            time.sleep(MIN_BATCH_LATENCY_SECONDS)
        with self._lock:
            remaining = len(self._buffer) + self._in_flight
        if remaining:
            self.on_failure(
                "logging.destination_flush_incomplete",
                RuntimeError(
                    "Background emitter could not deliver all buffered "
                    "log records before the flush deadline."
                ),
                destination=self.name,
                remaining=remaining,
            )

    def close(self) -> None:
        """Stop the worker without flushing."""
        self._closed.set()
        self._wake.set()
        if self._atexit_registered:
            atexit.unregister(self.flush)
            self._atexit_registered = False

    def restart(self) -> None:
        """Reset local state: drop buffered records, clear trip state,
        start a fresh worker on the next emit. A destination the manager
        already disabled cannot be revived here — use the manager-level
        restart, which rebuilds destinations."""
        with self._start_lock:
            self._closed.set()
            self._wake.set()
            self._worker = None
            self._pid = None
            with self._lock:
                self._buffer.clear()
                self._dropped = 0
            self._consecutive_failures = 0
            self._tripped.clear()

    def _ensure_worker(self) -> None:
        worker = self._worker
        if (
            worker is not None
            and worker.is_alive()
            and self._pid == os.getpid()
        ):
            return
        with self._start_lock:
            worker = self._worker
            if (
                worker is not None
                and worker.is_alive()
                and self._pid == os.getpid()
            ):
                return
            # Signal any superseded-but-alive worker before replacing its
            # generation event, so it exits instead of leaking.
            self._closed.set()
            closed = threading.Event()
            self._closed = closed
            thread = threading.Thread(
                target=self._run,
                args=(closed,),
                name=f"observability-emit-{self.name}",
                daemon=True,
            )
            self._pid = os.getpid()
            self._worker = thread
            thread.start()
            if not self._atexit_registered:
                atexit.register(self.flush)
                self._atexit_registered = True

    def _run(self, closed: threading.Event) -> None:
        while not closed.is_set():
            self._wake.wait(timeout=self.batch_latency_seconds)
            self._wake.clear()
            while not closed.is_set():
                outcome = self._drain_once()
                if outcome == DRAIN_EMPTY:
                    break
                if outcome == DRAIN_FAILED:
                    # Back off before hammering a failing sink again.
                    closed.wait(timeout=self.failure_backoff_seconds)

    def _drain_once(self) -> str:
        """Write one batch; returns a DRAIN_* outcome."""
        with self._lock:
            if not self._buffer:
                return DRAIN_EMPTY
            batch = [
                self._buffer.popleft()
                for _ in range(min(self.batch_size, len(self._buffer)))
            ]
            self._in_flight += len(batch)
        try:
            delivered = self._write_batch(batch)
        finally:
            with self._lock:
                self._in_flight -= len(batch)
        return DRAIN_DELIVERED if delivered else DRAIN_FAILED

    def _write_batch(self, batch: list[_LogRecord]) -> bool:
        try:
            emit_batch = getattr(self.wrapped, "emit_batch", None)
            if callable(emit_batch):
                emit_batch(batch)
            else:
                for payload, log_type, severity in batch:
                    self.wrapped.emit(
                        payload,
                        log_type=log_type,
                        severity=severity,
                    )
        except Exception as exc:
            self._consecutive_failures += 1
            self.on_failure(
                "logging.destination_emit_async",
                exc,
                destination=self.name,
                consecutive_failures=self._consecutive_failures,
                records_lost=len(batch),
            )
            if self._consecutive_failures >= self.failure_limit:
                self._tripped.set()
                self._closed.set()
            return False
        self._consecutive_failures = 0
        return True
