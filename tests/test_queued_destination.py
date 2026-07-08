"""QueuedLogDestination tests.

Determinism strategy: real listener threads are used, but sequencing is
via threading.Event only — close()/stop() drain to a sentinel enqueued
behind the records, so "close then assert delivered" is deterministic.
The only real-time waits are bounded joins that are themselves the
behavior under test, with generous ceilings.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from policyengine_observability.destinations.queued import (
    QueuedLogDestination,
)


class RecordingInner:
    name = "recording-inner"

    def __init__(self) -> None:
        self.calls = []

    def emit(self, payload, *, log_type, severity, timestamp=None) -> None:
        self.calls.append((payload, log_type, severity, timestamp))


class TimestampBlindInner:
    name = "timestamp-blind"

    def __init__(self) -> None:
        self.calls = []

    def emit(self, payload, *, log_type, severity) -> None:
        self.calls.append((payload, log_type, severity))


class BlockingInner:
    name = "blocking-inner"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def emit(self, payload, *, log_type, severity, timestamp=None) -> None:
        self.calls += 1
        self.entered.set()
        self.release.wait(10)


class FailingInner:
    name = "failing-inner"

    def __init__(self, fail_first: int | None = None) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.delivered = []

    def emit(self, payload, *, log_type, severity, timestamp=None) -> None:
        self.calls += 1
        if self.fail_first is None or self.calls <= self.fail_first:
            raise RuntimeError("write failed")
        self.delivered.append(payload)


def _queued(inner, **kwargs):
    failures = []
    destination = QueuedLogDestination(
        inner=inner,
        on_failure=lambda operation, exc, **fields: failures.append(
            (operation, fields)
        ),
        **kwargs,
    )
    return destination, failures


def _release_abandoned_listener(destination, blocking_inner) -> None:
    """Let an abandoned listener thread exit so tests do not leak it."""
    blocking_inner.release.set()
    try:
        destination._queue.put_nowait(None)  # the stdlib sentinel
    except Exception:
        pass


def test_close_drains_all_records_in_order_with_enqueue_timestamps() -> None:
    inner = RecordingInner()
    destination, failures = _queued(inner)
    before = datetime.now(UTC)

    for index in range(5):
        destination.emit({"index": index}, log_type="event", severity="INFO")
    destination.close(5.0)

    assert [payload["index"] for payload, *_ in inner.calls] == [
        0,
        1,
        2,
        3,
        4,
    ]
    for _payload, log_type, severity, timestamp in inner.calls:
        assert log_type == "event"
        assert severity == "INFO"
        assert timestamp.tzinfo is not None
        assert before <= timestamp <= datetime.now(UTC)
    assert failures == []


def test_timestamp_not_forwarded_to_blind_inner() -> None:
    inner = TimestampBlindInner()
    destination, failures = _queued(inner)

    destination.emit({"event": "x"}, log_type="event", severity="INFO")
    destination.close(5.0)

    assert inner.calls == [({"event": "x"}, "event", "INFO")]
    assert failures == []


def test_payload_snapshot_taken_at_enqueue() -> None:
    inner = RecordingInner()
    destination, _failures = _queued(inner)
    payload = {"nested": {"value": 1}}

    destination.emit(payload, log_type="event", severity="INFO")
    payload["nested"]["value"] = 2
    destination.close(5.0)

    ((delivered, *_),) = inner.calls
    assert delivered["nested"]["value"] == 1


def test_overflow_drops_newest_and_throttles_reports() -> None:
    inner = BlockingInner()
    destination, failures = _queued(inner, maxsize=10, drop_report_interval=3)

    destination.emit({"index": 0}, log_type="event", severity="INFO")
    assert inner.entered.wait(5)
    # The listener holds record 0; fill the 10-slot queue, then drop 4.
    started = time.monotonic()
    for index in range(1, 15):
        destination.emit({"index": index}, log_type="event", severity="INFO")
    assert time.monotonic() - started < 1.0

    drops = [fields for op, fields in failures if op == "logging.queue_drop"]
    assert [d["dropped_total"] for d in drops] == [1, 3]
    assert all(d["reason"] == "full" for d in drops)
    assert all(d["queue_maxsize"] == 10 for d in drops)
    assert destination._dropped == 4

    inner.release.set()
    destination.close(5.0)


def test_close_with_stuck_write_is_bounded_and_terminal() -> None:
    inner = BlockingInner()
    destination, failures = _queued(inner)
    destination.emit({"index": 0}, log_type="event", severity="INFO")
    assert inner.entered.wait(5)
    destination.emit({"index": 1}, log_type="event", severity="INFO")

    started = time.monotonic()
    destination.close(0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    timeouts = [
        fields
        for op, fields in failures
        if op == "logging.queue_close_timeout"
    ]
    assert len(timeouts) == 1
    assert timeouts[0]["pending_records"] >= 1

    destination.emit({"index": 2}, log_type="event", severity="INFO")
    drops = [fields for op, fields in failures if op == "logging.queue_drop"]
    assert drops[-1]["reason"] == "closed"

    _release_abandoned_listener(destination, inner)


def test_close_with_sentinel_blocked_by_full_queue_is_bounded() -> None:
    inner = BlockingInner()
    destination, failures = _queued(inner, maxsize=10)
    destination.emit({"index": 0}, log_type="event", severity="INFO")
    assert inner.entered.wait(5)
    for index in range(1, 11):
        destination.emit({"index": index}, log_type="event", severity="INFO")

    started = time.monotonic()
    destination.close(0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert any(op == "logging.queue_close_timeout" for op, _fields in failures)

    _release_abandoned_listener(destination, inner)


def test_listener_survives_write_failures_and_throttles_reports() -> None:
    inner = FailingInner(fail_first=3)
    destination, failures = _queued(inner)

    for index in range(5):
        destination.emit({"index": index}, log_type="event", severity="INFO")
    destination.close(5.0)

    assert [p["index"] for p in inner.delivered] == [3, 4]
    writes = [fields for op, fields in failures if op == "logging.queue_write"]
    # Throttle: failure 1 reports, failures 2-3 are under the interval.
    assert [w["write_failures_total"] for w in writes] == [1]
    assert writes[0]["destination"] == "failing-inner"


def test_emit_never_raises() -> None:
    inner = RecordingInner()
    destination, _failures = _queued(inner)
    destination.on_failure = _raise_on_failure

    poisoned = {}
    poisoned["self"] = poisoned  # RecursionError inside normalize
    destination.emit(poisoned, log_type="event", severity="INFO")

    destination.close(5.0)
    destination.emit({"event": "x"}, log_type="event", severity="INFO")

    blocked = BlockingInner()
    full_destination, _ = _queued(blocked, maxsize=10)
    full_destination.on_failure = _raise_on_failure
    for index in range(12):
        full_destination.emit(
            {"index": index}, log_type="event", severity="INFO"
        )

    blocked.release.set()
    full_destination.on_failure = lambda *args, **kwargs: None
    full_destination.close(5.0)


def _raise_on_failure(*args, **kwargs):
    raise RuntimeError("reporting channel is broken")


def test_atexit_registered_on_construction_unregistered_on_close(
    monkeypatch,
) -> None:
    registered = []
    unregistered = []
    monkeypatch.setattr(
        "policyengine_observability.destinations.queued.atexit.register",
        lambda func: registered.append(func),
    )
    monkeypatch.setattr(
        "policyengine_observability.destinations.queued.atexit.unregister",
        lambda func: unregistered.append(func),
    )
    inner = RecordingInner()

    destination, failures = _queued(inner)
    destination.close(5.0)
    destination.close(5.0)  # idempotent: no second stop, no reports

    assert registered == [destination.close]
    assert unregistered == [destination.close]
    assert failures == []


def test_close_forwards_to_inner_close_only_when_drained() -> None:
    class ClosableInner(RecordingInner):
        def __init__(self) -> None:
            super().__init__()
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    inner = ClosableInner()
    destination, _failures = _queued(inner)
    destination.emit({"event": "x"}, log_type="event", severity="INFO")
    destination.close(5.0)

    assert inner.closed == 1


def test_knobs_are_clamped() -> None:
    inner = RecordingInner()
    destination, _failures = _queued(
        inner, maxsize=float("inf"), close_timeout_seconds=-5
    )

    assert destination.maxsize == 1000
    assert destination.close_timeout_seconds == 0.0
    destination.close(5.0)


# ── Destination registry ─────────────────────────────────────────────────


def test_registry_builds_remote_wrapped_and_inline_bare() -> None:
    import json
    import logging

    from policyengine_observability.config import ObservabilityConfig
    from policyengine_observability.destinations.manager import (
        LogDestinationManager,
    )
    from policyengine_observability.destinations.registry import (
        _STRATEGIES,
        register_destination,
    )

    register_destination(
        "fake-remote",
        lambda **kwargs: RecordingInner(),
        transport="remote",
    )
    try:
        manager = LogDestinationManager(
            config=ObservabilityConfig(
                log_destinations=("fake_remote", "stdout")
            ),
            loggers={"event": logging.getLogger("test-registry")},
            serializer=json.dumps,
            on_failure=lambda *args, **kwargs: None,
        )
        manager.configure()

        remote, inline = manager.destinations
        assert isinstance(remote, QueuedLogDestination)
        assert remote.name == "queued_recording-inner"
        assert not isinstance(inline, QueuedLogDestination)
        manager.close()
    finally:
        _STRATEGIES.pop("fake_remote", None)


def test_registry_unknown_name_reports_config_failure() -> None:
    import json
    import logging

    from policyengine_observability.config import ObservabilityConfig
    from policyengine_observability.destinations.manager import (
        LogDestinationManager,
    )

    failures = []
    manager = LogDestinationManager(
        config=ObservabilityConfig(log_destinations=("nonexistent",)),
        loggers={"event": logging.getLogger("test-registry")},
        serializer=json.dumps,
        on_failure=lambda operation, exc, **fields: failures.append(
            (operation, fields)
        ),
    )
    manager.configure()

    assert any(
        op == "logging.destination_config"
        and fields.get("destination") == "nonexistent"
        for op, fields in failures
    )


def test_google_strategy_registered_as_remote_with_aliases() -> None:
    from policyengine_observability.destinations.registry import (
        destination_strategy,
    )

    canonical = destination_strategy("google_cloud_logging")
    assert canonical is not None
    assert canonical.transport == "remote"
    assert destination_strategy("google") is canonical
    assert destination_strategy(" Google-Cloud ") is canonical


# ── Manager integration ──────────────────────────────────────────────────


def test_manager_breaker_never_disables_queued_destination() -> None:
    import json
    import logging

    from policyengine_observability.config import ObservabilityConfig
    from policyengine_observability.destinations.manager import (
        LogDestinationManager,
    )

    inner = FailingInner()
    failures = []
    manager = LogDestinationManager(
        config=ObservabilityConfig(),
        loggers={"event": logging.getLogger("test-breaker")},
        serializer=json.dumps,
        on_failure=lambda operation, exc, **fields: failures.append(operation),
    )
    destination = QueuedLogDestination(
        inner=inner, on_failure=manager.on_failure
    )
    manager.destinations = [destination]
    manager.configured = True

    for _ in range(5):
        manager.emit({"event": "x"}, log_type="event", severity="INFO")
    destination.close(5.0)

    assert destination in manager.destinations
    assert "logging.destination_disabled" not in failures
    assert "logging.destination_emit" not in failures


def test_reconfigure_closes_previous_destinations() -> None:
    import json
    import logging

    from policyengine_observability.config import ObservabilityConfig
    from policyengine_observability.destinations.manager import (
        LogDestinationManager,
    )
    from policyengine_observability.destinations.registry import (
        _STRATEGIES,
        register_destination,
    )

    register_destination(
        "fake-remote",
        lambda **kwargs: RecordingInner(),
        transport="remote",
    )
    try:
        manager = LogDestinationManager(
            config=ObservabilityConfig(log_destinations=("fake_remote",)),
            loggers={"event": logging.getLogger("test-reconfigure")},
            serializer=json.dumps,
            on_failure=lambda *args, **kwargs: None,
        )
        manager.configure()
        first = manager.destinations[0]
        manager._consecutive_failures[id(first)] = 2

        manager.configure()

        assert first._closed is True
        assert manager._consecutive_failures == {}
        assert manager.destinations[0] is not first
        manager.close()
    finally:
        _STRATEGIES.pop("fake_remote", None)
