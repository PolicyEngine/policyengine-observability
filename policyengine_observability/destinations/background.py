from __future__ import annotations

import atexit
import os
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
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

# (normalized payload, log_type, severity, enqueued-at RFC3339 timestamp)
_LogRecord = tuple[dict[str, Any], str, str, str]

# Fork handling: threads (including workers holding locks) do not survive
# fork, so every instance must rebuild its synchronization primitives and
# drop the inherited buffer copy (the parent's worker will deliver it) in
# the child while it is still single-threaded.
_INSTANCES: weakref.WeakSet[BackgroundEmitDestination] = weakref.WeakSet()


def _reset_instances_after_fork() -> None:  # pragma: no cover - fork hook
    for destination in list(_INSTANCES):
        destination._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_instances_after_fork)


class BackgroundEmitDestination:
    """Decouples log acceptance from emission.

    ``emit()`` normalizes the payload (snapshotting it against caller
    mutation), stamps the enqueue time, appends the record to a bounded
    in-memory buffer, and returns immediately — it never blocks on the
    network. When the buffer is full the oldest record is dropped
    (counted and reported, throttled), keeping the freshest records. A
    daemon worker wakes when the buffer becomes non-empty, naps briefly
    to coalesce a batch, and drains to the wrapped destination
    (preferring its ``emit_batch``), backing off between failed batches;
    after ``failure_limit`` consecutive failed batches the wrapper trips
    and subsequent ``emit()`` calls raise so the manager's circuit
    breaker disables it. Recovery happens at the manager level
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
        self._wrapped_emit_batch = getattr(wrapped, "emit_batch", None)
        if not callable(self._wrapped_emit_batch):
            self._wrapped_emit_batch = None
        self._buffer: deque[_LogRecord] = deque(maxlen=self.queue_size)
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._wake = threading.Event()
        self._tripped = threading.Event()
        self._stopped = threading.Event()
        self._closed = threading.Event()
        self._worker: threading.Thread | None = None
        self._pid: int | None = None
        self._consecutive_failures = 0
        self._dropped = 0
        self._fork_dropped = 0
        self._in_flight = 0
        self._atexit_registered = False
        _INSTANCES.add(self)

    def emit(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> None:
        if self._stopped.is_set():
            # Only reachable through a stale reference during a manager
            # reconfigure swap; the manager no longer routes here.
            return
        if self._tripped.is_set():
            raise RuntimeError(
                f"Background emitter for {self.name} is tripped after "
                f"{self.failure_limit} consecutive batch failures."
            )
        self._ensure_worker()
        # Snapshot now: the caller may keep mutating nested structures
        # after this returns, and serialization happens on the worker.
        # The timestamp preserves event time against emission delay.
        record = (
            normalize_payload(payload),
            log_type,
            severity,
            datetime.now(UTC).isoformat(),
        )
        dropped_total: int | None = None
        fork_dropped: int | None = None
        with self._lock:
            was_empty = not self._buffer
            if len(self._buffer) == self.queue_size:
                self._dropped += 1
                if (
                    self._dropped == 1
                    or self._dropped % DROP_REPORT_EVERY == 0
                ):
                    dropped_total = self._dropped
            self._buffer.append(record)
            if self._fork_dropped:
                fork_dropped = self._fork_dropped
                self._fork_dropped = 0
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
        if fork_dropped is not None:
            self.on_failure(
                "logging.destination_fork_buffer_dropped",
                RuntimeError(
                    "Dropped log records inherited across a fork; the "
                    "parent process delivers its own copy."
                ),
                destination=self.name,
                dropped_total=fork_dropped,
            )
        if was_empty:
            self._wake.set()

    def flush(self, deadline_seconds: float | None = None) -> None:
        """Drain the buffer from the caller's thread. Bounded by a soft
        deadline (a blocking write in progress can overrun it by one
        write budget); waits for a worker-held in-flight batch and
        reports any undelivered remainder."""
        if deadline_seconds is None:
            deadline_seconds = self.flush_deadline_seconds
        deadline = time.monotonic() + max(0.0, deadline_seconds)
        while not self._tripped.is_set():
            outcome = self._drain_once(self._closed)
            if outcome == DRAIN_EMPTY:
                with self._lock:
                    idle = not self._buffer and self._in_flight == 0
                if idle:
                    # Fully drained: nothing to report, even if a record
                    # arrives after this instant.
                    return
            if time.monotonic() >= deadline:
                break
            if outcome != DRAIN_DELIVERED:
                # Empty-but-in-flight or a failed batch: brief pause.
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
        """Stop permanently: no further records are accepted, the worker
        exits, and undelivered records are reported and discarded."""
        self._stopped.set()
        self._closed.set()
        self._wake.set()
        with self._lock:
            remaining = len(self._buffer) + self._in_flight
            self._buffer.clear()
        if remaining:
            self.on_failure(
                "logging.destination_closed_pending",
                RuntimeError(
                    "Background emitter closed with undelivered log records."
                ),
                destination=self.name,
                remaining=remaining,
            )
        if self._atexit_registered:
            atexit.unregister(self.flush)
            self._atexit_registered = False
        wrapped_close = getattr(self.wrapped, "close", None)
        if callable(wrapped_close):
            try:
                wrapped_close()
            except Exception as exc:
                self.on_failure(
                    "logging.destination_close",
                    exc,
                    destination=self.name,
                )

    def _reset_after_fork(self) -> None:
        """Runs in a forked child while it is single-threaded: parent
        threads (possibly holding our locks) do not exist here, and the
        buffer is a copy the parent will deliver itself."""
        inherited = len(self._buffer)
        # Signal the superseded generation first: after a real fork no
        # thread is listening (harmless); on the belt-path pid check a
        # live stale worker exits instead of leaking.
        self._closed.set()
        self._wake.set()
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._buffer = deque(maxlen=self.queue_size)
        self._in_flight = 0
        self._consecutive_failures = 0
        self._worker = None
        self._pid = None
        self._fork_dropped += inherited

    def _ensure_worker(self) -> None:
        if self._pid is not None and self._pid != os.getpid():
            # Belt for exotic fork paths that bypassed the fork hook.
            self._reset_after_fork()
        worker = self._worker
        if worker is not None and worker.is_alive():
            return
        with self._start_lock:
            worker = self._worker
            if worker is not None and worker.is_alive():
                return
            if self._stopped.is_set():
                return
            # Signal any superseded-but-alive worker before replacing its
            # generation event, so it exits instead of leaking.
            self._closed.set()
            self._wake.set()
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
        try:
            while not closed.is_set():
                # Idle costs nothing: emit() wakes us on the buffer's
                # empty-to-non-empty transition.
                self._wake.wait()
                self._wake.clear()
                if closed.is_set():
                    break
                # Nap briefly so nearby records coalesce into one batch.
                closed.wait(timeout=self.batch_latency_seconds)
                while not closed.is_set():
                    outcome = self._drain_once(closed)
                    if outcome == DRAIN_EMPTY:
                        break
                    if outcome == DRAIN_FAILED:
                        closed.wait(timeout=self.failure_backoff_seconds)
        except BaseException as exc:  # worker must never die silently
            try:
                self.on_failure(
                    "logging.destination_worker_crashed",
                    exc,
                    destination=self.name,
                )
            except Exception:  # pragma: no cover - reporting best-effort
                pass

    def _drain_once(self, closed: threading.Event | None = None) -> str:
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
            delivered = self._write_batch(batch, closed)
        finally:
            with self._lock:
                self._in_flight -= len(batch)
        return DRAIN_DELIVERED if delivered else DRAIN_FAILED

    def _write_batch(
        self,
        batch: list[_LogRecord],
        closed: threading.Event | None,
    ) -> bool:
        try:
            if self._wrapped_emit_batch is not None:
                self._wrapped_emit_batch(batch)
            else:
                for payload, log_type, severity, _timestamp in batch:
                    self.wrapped.emit(
                        payload,
                        log_type=log_type,
                        severity=severity,
                    )
        except Exception as exc:
            stale = closed is not None and closed is not self._closed
            if stale:
                # A superseded generation's late failure must not poison
                # the current generation's breaker state.
                return False
            tripped_now = False
            stranded = 0
            with self._lock:
                self._consecutive_failures += 1
                failures = self._consecutive_failures
                if (
                    failures >= self.failure_limit
                    and not self._tripped.is_set()
                ):
                    tripped_now = True
                    stranded = len(self._buffer)
            if tripped_now:
                # Trip BEFORE reporting so the failure report cannot be
                # enqueued into this now-doomed buffer.
                self._tripped.set()
                self._closed.set()
                self._wake.set()
            fields: dict[str, Any] = {
                "destination": self.name,
                "consecutive_failures": failures,
                "records_lost": len(batch),
            }
            if tripped_now:
                fields["stranded"] = stranded
            self.on_failure(
                "logging.destination_emit_async",
                exc,
                **fields,
            )
            return False
        with self._lock:
            self._consecutive_failures = 0
        return True
