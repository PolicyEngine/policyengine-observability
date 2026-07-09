"""Bounded, best-effort background delivery for remote log destinations.

``QueuedLogDestination`` wraps any ``LogDestination`` so the caller's
thread only ever enqueues: a stdlib ``logging.handlers.QueueListener``
thread performs the actual writes, so a degraded sink can never stall a
request. Delivery is best-effort by contract — the stdout sibling
destination is the durable record — so a full queue drops the newest
record and counts it, and there is deliberately no circuit breaker,
retry queue, or recovery machinery in this component.

Mutable state census (any addition needs design review):

1. ``_queue``   — thread-safe by construction (``queue.Queue``).
2. ``_listener``— started once in ``__init__``, stopped once in ``close``.
3. ``_drops``   — best-effort drop counter with its throttle state.
4. ``_closed``  — one-way flag flipped by ``close``.

(The handler's write-failure counter is confined to the listener
thread, so it is not shared mutable state.)

Accepted races, all bounded and within the best-effort contract:

- An ``emit`` that passes the ``_closed`` check while ``close`` runs can
  lose that one record uncounted.
- When ``close`` times out, the daemon listener thread is abandoned; it
  keeps draining in the background until process exit.
- A process that forks after construction (e.g. gunicorn ``--preload``)
  inherits a dead listener; records drop with ``reason="full"`` until
  the child calls ``restart_observability()`` from a post-fork hook.
  There are deliberately no fork hooks or pid checks here.
"""

from __future__ import annotations

import atexit
import queue as queue_module
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import QueueListener
from typing import Any

from ..config import (
    DEFAULT_LOG_QUEUE_CLOSE_TIMEOUT_SECONDS,
    DEFAULT_LOG_QUEUE_MAXSIZE,
)
from .base import (
    LogDestination,
    accepts_keyword,
    clamped,
    close_destination,
    normalize_payload,
    safe_report,
)

MIN_QUEUE_MAXSIZE = 10
MAX_QUEUE_MAXSIZE = 100_000
MIN_CLOSE_TIMEOUT_SECONDS = 0.0
MAX_CLOSE_TIMEOUT_SECONDS = 30.0
DROP_REPORT_INTERVAL = 100
WRITE_FAILURE_REPORT_INTERVAL = 100


@dataclass(frozen=True, slots=True)
class _QueuedRecord:
    payload: Any
    log_type: str
    severity: str
    enqueued_at: datetime


class _ThrottledCounter:
    """Count occurrences; say when one should be reported.

    The shared throttle policy for failure-path reporting: the first
    occurrence always reports, then every ``interval``-th, so a
    persistent problem stays visible without flooding the internal-error
    channel.
    """

    __slots__ = ("count", "interval")

    def __init__(self, interval: int) -> None:
        self.interval = max(1, int(interval))
        self.count = 0

    def tick(self) -> int | None:
        """Increment; return the count when this occurrence reports."""
        self.count += 1
        if self.count == 1 or self.count % self.interval == 0:
            return self.count
        return None


class _QueuedRecordHandler:
    """Duck-typed QueueListener handler: only ``handle`` is ever called.

    The failure counter is confined to the listener thread. A write
    failure must never kill the listener, so everything below the emit
    is guarded; ``BaseException`` is deliberately not caught (swallowing
    ``SystemExit`` on a worker thread is worse than losing the queue —
    the consequences of a dead listener are bounded to counted drops).
    """

    def __init__(
        self,
        inner: LogDestination,
        on_failure: Callable[..., None],
        *,
        forward_timestamp: bool,
        report_interval: int = WRITE_FAILURE_REPORT_INTERVAL,
    ) -> None:
        self.inner = inner
        self.on_failure = on_failure
        self.forward_timestamp = forward_timestamp
        self.failures = _ThrottledCounter(report_interval)

    def handle(self, record: _QueuedRecord) -> None:
        try:
            if self.forward_timestamp:
                self.inner.emit(
                    record.payload,
                    log_type=record.log_type,
                    severity=record.severity,
                    timestamp=record.enqueued_at,
                )
            else:
                self.inner.emit(
                    record.payload,
                    log_type=record.log_type,
                    severity=record.severity,
                )
        except Exception as exc:
            count = self.failures.tick()
            if count is None:
                return
            safe_report(
                self.on_failure,
                "logging.queue_write",
                exc,
                destination=getattr(self.inner, "name", None),
                log_type=record.log_type,
                write_failures_total=count,
            )


class _BoundedQueueListener(QueueListener):
    """QueueListener whose stop can be given a hard deadline.

    The stdlib ``stop()`` enqueues its sentinel with ``put_nowait``
    (which raises on a jammed bounded queue) and then joins the worker
    thread without a timeout. Here one monotonic deadline covers both
    the blocking sentinel put and the join; on expiry the daemon thread
    is abandoned and ``False`` is returned.
    """

    def stop(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        if timeout is None:
            super().stop()
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            self.queue.put(
                self._sentinel,
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except queue_module.Full:
            pass
        thread.join(max(0.0, deadline - time.monotonic()))
        stopped = not thread.is_alive()
        self._thread = None
        return stopped


class QueuedLogDestination:
    def __init__(
        self,
        *,
        inner: LogDestination,
        on_failure: Callable[..., None],
        maxsize: float = DEFAULT_LOG_QUEUE_MAXSIZE,
        close_timeout_seconds: float = DEFAULT_LOG_QUEUE_CLOSE_TIMEOUT_SECONDS,
        drop_report_interval: int = DROP_REPORT_INTERVAL,
    ) -> None:
        self.inner = inner
        self.on_failure = on_failure
        self.name = f"queued_{getattr(inner, 'name', 'destination')}"
        self.maxsize = int(
            clamped(
                maxsize,
                low=MIN_QUEUE_MAXSIZE,
                high=MAX_QUEUE_MAXSIZE,
                default=DEFAULT_LOG_QUEUE_MAXSIZE,
            )
        )
        self.close_timeout_seconds = clamped(
            close_timeout_seconds,
            low=MIN_CLOSE_TIMEOUT_SECONDS,
            high=MAX_CLOSE_TIMEOUT_SECONDS,
            default=DEFAULT_LOG_QUEUE_CLOSE_TIMEOUT_SECONDS,
        )
        self._queue: queue_module.Queue[_QueuedRecord | None] = (
            queue_module.Queue(self.maxsize)
        )
        self._listener = _BoundedQueueListener(
            self._queue,
            _QueuedRecordHandler(
                inner,
                on_failure,
                forward_timestamp=accepts_keyword(inner.emit, "timestamp"),
            ),
        )
        self._drops = _ThrottledCounter(drop_report_interval)
        self._closed = False
        # Construction happens at configure time on the startup thread,
        # never lazily on a request thread.
        self._listener.start()
        atexit.register(self.close)

    def emit(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> None:
        try:
            if self._closed:
                self._record_drop("closed", log_type)
                return
            record = _QueuedRecord(
                # Snapshot now: callers keep mutating nested structures
                # after emit returns, and the write happens later on the
                # listener thread. The enqueue time becomes the entry
                # timestamp so delayed writes keep event time.
                payload=normalize_payload(payload),
                log_type=log_type,
                severity=severity,
                enqueued_at=datetime.now(UTC),
            )
            try:
                self._queue.put_nowait(record)
            except queue_module.Full:
                self._record_drop("full", log_type)
        except Exception as exc:
            self._record_drop("exception", log_type, exc=exc)

    def close(self, deadline_seconds: float | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self.close)
        deadline = clamped(
            deadline_seconds,
            low=MIN_CLOSE_TIMEOUT_SECONDS,
            high=MAX_CLOSE_TIMEOUT_SECONDS,
            default=self.close_timeout_seconds,
        )
        drained = self._listener.stop(timeout=deadline)
        if not drained:
            safe_report(
                self.on_failure,
                "logging.queue_close_timeout",
                TimeoutError(
                    "Observability log queue did not drain before "
                    "the close deadline; remaining records are lost."
                ),
                destination=self.name,
                deadline_seconds=deadline,
                pending_records=self._queue.qsize(),
            )
            # The abandoned listener may still be mid-write; leave the
            # inner destination alone rather than closing it underneath
            # an active write.
            return
        close_destination(self.inner, on_failure=self.on_failure)

    def _record_drop(
        self,
        reason: str,
        log_type: str,
        exc: BaseException | None = None,
    ) -> None:
        count = self._drops.tick()
        if count is None:
            return
        safe_report(
            self.on_failure,
            "logging.queue_drop",
            exc or RuntimeError("Observability log queue dropped a record."),
            destination=self.name,
            log_type=log_type,
            reason=reason,
            dropped_total=count,
            queue_maxsize=self.maxsize,
        )
