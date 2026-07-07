from __future__ import annotations

import atexit
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .base import LogDestination

DEFAULT_QUEUE_SIZE = 1000
DEFAULT_BATCH_SIZE = 10
DEFAULT_BATCH_LATENCY_SECONDS = 0.25
DEFAULT_FLUSH_DEADLINE_SECONDS = 5.0
DEFAULT_FAILURE_LIMIT = 3
DROP_REPORT_EVERY = 100

_LogRecord = tuple[dict[str, Any], str, str]


class BackgroundEmitDestination:
    """Decouples log acceptance from emission.

    ``emit()`` appends to a bounded in-memory buffer and returns
    immediately — it never blocks on the network and never raises for
    sink trouble. A daemon worker drains the buffer in batches to the
    wrapped destination (preferring its ``emit_batch``). Overflow drops
    the newest record and counts it; after ``failure_limit`` consecutive
    failed batches the wrapper trips, and subsequent ``emit()`` calls
    raise so the manager's existing circuit breaker disables it through
    the same path as a synchronous destination (stdout fallback
    preserved).

    The worker starts lazily on first emit and is pid-aware, so a forked
    or snapshot-restored process (where threads do not survive) starts a
    fresh worker automatically; ``restart()`` additionally clears the
    buffer and trip state for consumers that restore process memory.
    """

    def __init__(
        self,
        wrapped: LogDestination,
        *,
        on_failure: Callable[..., None],
        queue_size: int = DEFAULT_QUEUE_SIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_latency_seconds: float = DEFAULT_BATCH_LATENCY_SECONDS,
        flush_deadline_seconds: float = DEFAULT_FLUSH_DEADLINE_SECONDS,
        failure_limit: int = DEFAULT_FAILURE_LIMIT,
    ) -> None:
        self.wrapped = wrapped
        self.name = getattr(wrapped, "name", "background")
        self.on_failure = on_failure
        self.queue_size = queue_size
        self.batch_size = max(1, batch_size)
        self.batch_latency_seconds = batch_latency_seconds
        self.flush_deadline_seconds = flush_deadline_seconds
        self.failure_limit = failure_limit
        self._buffer: deque[_LogRecord] = deque()
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._wake = threading.Event()
        self._tripped = threading.Event()
        self._closed = threading.Event()
        self._worker: threading.Thread | None = None
        self._pid: int | None = None
        self._consecutive_failures = 0
        self._dropped = 0
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
        dropped_total: int | None = None
        with self._lock:
            if len(self._buffer) >= self.queue_size:
                self._dropped += 1
                if (
                    self._dropped == 1
                    or self._dropped % DROP_REPORT_EVERY == 0
                ):
                    dropped_total = self._dropped
            else:
                self._buffer.append((payload, log_type, severity))
        if dropped_total is not None:
            self.on_failure(
                "logging.destination_queue_overflow",
                RuntimeError(
                    "Background emitter buffer is full; dropping newest "
                    "log records."
                ),
                destination=self.name,
                dropped_total=dropped_total,
            )
        self._wake.set()

    def flush(self, deadline_seconds: float | None = None) -> None:
        """Drain the buffer from the caller's thread, bounded by a
        deadline; reports any undelivered remainder."""
        if deadline_seconds is None:
            deadline_seconds = self.flush_deadline_seconds
        deadline = time.monotonic() + deadline_seconds
        while not self._tripped.is_set() and time.monotonic() < deadline:
            if not self._drain_once():
                break
        with self._lock:
            remaining = len(self._buffer)
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

    def restart(self) -> None:
        """Reset for a process whose memory was restored or forked:
        drop buffered records, clear trip state, start a fresh worker on
        the next emit."""
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
            closed = threading.Event()
            self._closed = closed
            self._wake = threading.Event()
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
            while not closed.is_set() and self._drain_once():
                pass

    def _drain_once(self) -> bool:
        """Write one batch; returns True while more work may remain."""
        with self._lock:
            if not self._buffer:
                return False
            batch = [
                self._buffer.popleft()
                for _ in range(min(self.batch_size, len(self._buffer)))
            ]
        return self._write_batch(batch)

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
        except BaseException as exc:
            self._consecutive_failures += 1
            self.on_failure(
                "logging.destination_emit_async",
                exc,
                destination=self.name,
                consecutive_failures=self._consecutive_failures,
            )
            if self._consecutive_failures >= self.failure_limit:
                self._tripped.set()
                self._closed.set()
            return not self._tripped.is_set()
        self._consecutive_failures = 0
        return True
