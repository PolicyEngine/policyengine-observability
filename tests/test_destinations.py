from __future__ import annotations

from policyengine_observability.destinations import (
    GoogleCloudLoggingDestination,
    google_cloud_logging,
    normalize_payload,
)


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("cannot stringify")


class FakeResource:
    def _to_dict(self) -> dict:
        return {"type": "global", "labels": {}}


class FakeLogger:
    def __init__(self) -> None:
        self.full_name = "projects/resolved-project/logs/test-log"
        self.default_resource = FakeResource()


class FakeGapicApi:
    def __init__(self) -> None:
        self.calls = []

    def write_log_entries(self, *, request, retry, timeout) -> None:
        self.calls.append((request, retry, timeout))


class FakeLoggingApi:
    def __init__(self, *, gapic: bool) -> None:
        self.calls = []
        if gapic:
            self._gapic_api = FakeGapicApi()

    def write_entries(self, entries, *, partial_success) -> None:
        self.calls.append((entries, partial_success))


class FakeClient:
    def __init__(self, *, gapic: bool = True) -> None:
        self.project = "resolved-project"
        self.fake_logger = FakeLogger()
        self.log_names = []
        self.logging_api = FakeLoggingApi(gapic=gapic)

    def logger(self, log_name: str) -> FakeLogger:
        self.log_names.append(log_name)
        return self.fake_logger


def _destination(monkeypatch, client, **kwargs):
    monkeypatch.setattr(
        google_cloud_logging,
        "load_google_credentials",
        lambda *, prefer_workload_identity: None,
    )
    monkeypatch.setattr(
        google_cloud_logging,
        "configure_google_application_credentials",
        lambda: None,
    )
    return GoogleCloudLoggingDestination(
        project=None,
        log_name="policyengine-observability",
        client_factory=lambda _project, _credentials: client,
        **kwargs,
    )


def test_normalize_payload_recursively_stringifies_unsafe_values() -> None:
    normalized = normalize_payload(
        {
            "keep": "value",
            "drop_none": None,
            "bytes": b"value",
            "list": [1, Unprintable()],
            "nested": {"object": object()},
        }
    )

    assert normalized["keep"] == "value"
    assert normalized["drop_none"] is None
    assert normalized["bytes"] == "value"
    assert normalized["list"] == [1, "<unprintable Unprintable>"]
    assert normalized["nested"]["object"].startswith("<object object at ")


def test_google_destination_writes_bounded_gapic_entry(monkeypatch) -> None:
    client = FakeClient()
    destination = _destination(monkeypatch, client)

    destination.emit(
        {
            "schema_version": "policyengine.observability.request.v1",
            "service_name": "svc",
            "service_role": "api",
            "environment": "production",
            "request_id": "request-1",
            "trace_id": "abc123",
            "span_id": "def456",
            "path": "/calculate",
            "object": object(),
        },
        log_type="request",
        severity="ERROR",
    )

    assert client.log_names == ["policyengine-observability"]
    assert client.logging_api.calls == []
    request, retry, timeout = client.logging_api._gapic_api.calls[0]
    # Transient errors retry only inside the bounded write budget.
    assert retry is not None
    assert timeout == 5.0
    assert request.partial_success is True
    (entry,) = request.entries
    from google.logging.type.log_severity_pb2 import LogSeverity

    assert entry.log_name == "projects/resolved-project/logs/test-log"
    assert entry.resource.type == "global"
    assert LogSeverity.Name(entry.severity) == "ERROR"
    assert entry.trace == "projects/resolved-project/traces/abc123"
    assert entry.span_id == "def456"
    assert dict(entry.labels) == {
        "log_type": "request",
        "service_name": "svc",
        "service_role": "api",
        "environment": "production",
        "schema_version": "policyengine.observability.request.v1",
    }
    payload = dict(entry.json_payload)
    assert payload["path"] == "/calculate"
    assert payload["object"].startswith("<object object at ")


def test_google_destination_falls_back_to_unbounded_write_entries(
    monkeypatch,
) -> None:
    client = FakeClient(gapic=False)
    destination = _destination(monkeypatch, client)

    destination.emit(
        {"trace_id": None, "event": "x"},
        log_type="event",
        severity="info",
    )

    entries, partial_success = client.logging_api.calls[0]
    assert partial_success is True
    (entry,) = entries
    assert entry["severity"] == "INFO"
    assert entry["resource"] == {"type": "global", "labels": {}}
    assert "trace" not in entry
    assert "spanId" not in entry


def test_google_destination_respects_configured_timeout(monkeypatch) -> None:
    client = FakeClient()
    destination = _destination(monkeypatch, client, timeout_seconds=0.5)

    destination.emit({"event": "x"}, log_type="event", severity="INFO")

    _request, _retry, timeout = client.logging_api._gapic_api.calls[0]
    assert timeout == 0.5


def test_google_destination_emit_batch_writes_one_call(monkeypatch) -> None:
    client = FakeClient()
    destination = _destination(monkeypatch, client)

    destination.emit_batch(
        [
            ({"event": "a"}, "event", "INFO"),
            ({"event": "b"}, "request", "WARNING"),
        ]
    )
    destination.emit_batch([])

    assert len(client.logging_api._gapic_api.calls) == 1
    request, _retry, _timeout = client.logging_api._gapic_api.calls[0]
    from google.logging.type.log_severity_pb2 import LogSeverity

    assert len(request.entries) == 2
    assert LogSeverity.Name(request.entries[1].severity) == "WARNING"


def test_google_destination_handles_plain_mapping_resource(
    monkeypatch,
) -> None:
    client = FakeClient(gapic=False)
    client.fake_logger.default_resource = {
        "type": "gce_instance",
        "labels": {},
    }
    destination = _destination(monkeypatch, client)

    destination.emit({"event": "x"}, log_type="event", severity="INFO")

    (entry,), _ = client.logging_api.calls[0]
    assert entry["resource"] == {"type": "gce_instance", "labels": {}}


# ── Destination circuit breaker ──────────────────────────────────────────


class FlakyDestination:
    name = "flaky"

    def __init__(self, fail_first: int | None = None) -> None:
        self.calls = 0
        self.fail_first = fail_first

    def emit(self, payload, *, log_type, severity) -> None:
        self.calls += 1
        if self.fail_first is None or self.calls <= self.fail_first:
            raise RuntimeError("emit failed")


class RecordingDestination:
    name = "recording"

    def __init__(self) -> None:
        self.payloads = []

    def emit(self, payload, *, log_type, severity) -> None:
        self.payloads.append(payload)


def _manager(destinations):
    import json
    import logging

    from policyengine_observability.config import ObservabilityConfig
    from policyengine_observability.destinations.manager import (
        LogDestinationManager,
    )

    failures = []
    manager = LogDestinationManager(
        config=ObservabilityConfig(),
        loggers={"event": logging.getLogger("test-destinations")},
        serializer=json.dumps,
        on_failure=lambda operation, exc, **fields: failures.append(
            (operation, fields)
        ),
    )
    manager.destinations = list(destinations)
    manager.configured = True
    return manager, failures


def test_destination_disabled_after_consecutive_emit_failures() -> None:
    from policyengine_observability.destinations.manager import (
        DESTINATION_FAILURE_LIMIT,
    )

    flaky = FlakyDestination()
    healthy = RecordingDestination()
    manager, failures = _manager([flaky, healthy])

    for _ in range(DESTINATION_FAILURE_LIMIT + 2):
        manager.emit({"event": "x"}, log_type="event", severity="INFO")

    assert flaky.calls == DESTINATION_FAILURE_LIMIT
    assert flaky not in manager.destinations
    assert len(healthy.payloads) == DESTINATION_FAILURE_LIMIT + 2
    assert any(op == "logging.destination_disabled" for op, _ in failures)


def test_emit_success_resets_the_failure_counter() -> None:
    from policyengine_observability.destinations.manager import (
        DESTINATION_FAILURE_LIMIT,
    )

    flaky = FlakyDestination(fail_first=DESTINATION_FAILURE_LIMIT - 1)
    manager, failures = _manager([flaky])

    for _ in range(DESTINATION_FAILURE_LIMIT + 2):
        manager.emit({"event": "x"}, log_type="event", severity="INFO")

    assert flaky in manager.destinations
    counts = [
        fields["consecutive_failures"]
        for op, fields in failures
        if op == "logging.destination_emit"
    ]
    assert max(counts) == DESTINATION_FAILURE_LIMIT - 1


def test_sole_disabled_destination_falls_back_to_stdout() -> None:
    from policyengine_observability.destinations.manager import (
        DESTINATION_FAILURE_LIMIT,
    )
    from policyengine_observability.destinations.stdout import (
        StdoutJsonDestination,
    )

    flaky = FlakyDestination()
    manager, failures = _manager([flaky])

    for _ in range(DESTINATION_FAILURE_LIMIT + 1):
        manager.emit({"event": "x"}, log_type="event", severity="INFO")

    assert flaky not in manager.destinations
    assert any(
        isinstance(destination, StdoutJsonDestination)
        for destination in manager.destinations
    )


# ── Stdout output formats ────────────────────────────────────────────────


class RecordingLogger:
    def __init__(self) -> None:
        self.lines = []

    def info(self, message) -> None:
        self.lines.append(("info", message))

    def warning(self, message) -> None:
        self.lines.append(("warning", message))

    def error(self, message) -> None:
        self.lines.append(("error", message))


def _stdout_destination(**kwargs):
    import json

    from policyengine_observability.destinations.stdout import (
        StdoutJsonDestination,
    )

    logger = RecordingLogger()
    destination = StdoutJsonDestination(
        loggers={"event": logger},
        serializer=json.dumps,
        **kwargs,
    )
    return destination, logger


def test_stdout_google_format_maps_agent_keys() -> None:
    import json

    destination, logger = _stdout_destination(
        output_format="google",
        google_cloud_project="central-project",
    )

    destination.emit(
        {
            "created_at": "2026-07-07T00:00:00+00:00",
            "trace_id": "abc123",
            "span_id": "def456",
            "service_name": "svc",
            "path": "/calculate",
        },
        log_type="event",
        severity="WARNING",
    )

    level, message = logger.lines[0]
    line = json.loads(message)
    assert level == "warning"
    assert line["severity"] == "WARNING"
    assert line["time"] == "2026-07-07T00:00:00+00:00"
    assert (
        line["logging.googleapis.com/trace"]
        == "projects/central-project/traces/abc123"
    )
    assert line["logging.googleapis.com/spanId"] == "def456"
    assert line["logging.googleapis.com/labels"] == {
        "log_type": "event",
        "service_name": "svc",
    }
    assert line["path"] == "/calculate"


def test_stdout_google_format_omits_trace_without_project() -> None:
    import json

    destination, logger = _stdout_destination(output_format="google")

    destination.emit(
        {"trace_id": "abc123"},
        log_type="event",
        severity="ERROR",
    )

    _level, message = logger.lines[0]
    line = json.loads(message)
    assert "logging.googleapis.com/trace" not in line
    assert "time" not in line
    assert line["severity"] == "ERROR"


def test_stdout_plain_format_adds_no_agent_keys() -> None:
    import json

    destination, logger = _stdout_destination()

    destination.emit(
        {"trace_id": "abc123", "severity": "INFO"},
        log_type="event",
        severity="INFO",
    )

    level, message = logger.lines[0]
    line = json.loads(message)
    assert level == "info"
    assert line == {"trace_id": "abc123", "severity": "INFO"}


def test_manager_stdout_fallback_carries_configured_format() -> None:
    import json
    import logging

    from policyengine_observability.config import ObservabilityConfig
    from policyengine_observability.destinations.manager import (
        LogDestinationManager,
    )

    manager = LogDestinationManager(
        config=ObservabilityConfig(
            stdout_format="google",
            google_cloud_project="central-project",
        ),
        loggers={"event": logging.getLogger("test-stdout-format")},
        serializer=json.dumps,
        on_failure=lambda *args, **kwargs: None,
    )

    destination = manager._stdout_destination()

    assert destination.output_format == "google"
    assert destination.google_cloud_project == "central-project"


# ── Background emitter ───────────────────────────────────────────────────


class BatchRecordingDestination:
    name = "batch-recording"

    def __init__(self) -> None:
        self.batches = []
        self.single_emits = []

    def emit(self, payload, *, log_type, severity) -> None:
        self.single_emits.append((payload, log_type, severity))

    def emit_batch(self, records) -> None:
        self.batches.append(list(records))


def _background(wrapped, **kwargs):
    from policyengine_observability.destinations.background import (
        BackgroundEmitDestination,
    )

    failures = []
    destination = BackgroundEmitDestination(
        wrapped,
        on_failure=lambda operation, exc, **fields: failures.append(
            (operation, fields)
        ),
        **kwargs,
    )
    return destination, failures


def _inline(destination, monkeypatch):
    """Disable the worker thread so tests drive draining synchronously."""
    monkeypatch.setattr(destination, "_ensure_worker", lambda: None)
    return destination


def test_background_emit_enqueues_without_writing(monkeypatch) -> None:
    wrapped = BatchRecordingDestination()
    destination, _failures = _background(wrapped, batch_size=2)
    _inline(destination, monkeypatch)

    for index in range(3):
        destination.emit({"event": index}, log_type="event", severity="INFO")

    assert wrapped.batches == []
    assert len(destination._buffer) == 3


def test_background_drain_prefers_emit_batch(monkeypatch) -> None:
    wrapped = BatchRecordingDestination()
    destination, _failures = _background(wrapped, batch_size=2)
    _inline(destination, monkeypatch)

    for index in range(3):
        destination.emit({"event": index}, log_type="event", severity="INFO")
    while destination._drain_once() != "empty":
        pass

    assert [len(batch) for batch in wrapped.batches] == [2, 1]
    assert wrapped.single_emits == []
    assert wrapped.batches[0][0] == ({"event": 0}, "event", "INFO")


def test_background_drain_falls_back_to_single_emits(monkeypatch) -> None:
    wrapped = RecordingDestination()
    destination, _failures = _background(wrapped, batch_size=2)
    _inline(destination, monkeypatch)

    destination.emit({"event": "a"}, log_type="event", severity="INFO")
    while destination._drain_once() != "empty":
        pass

    assert wrapped.payloads == [{"event": "a"}]


def test_background_overflow_drops_oldest_and_reports(monkeypatch) -> None:
    wrapped = BatchRecordingDestination()
    destination, failures = _background(wrapped, queue_size=2)
    _inline(destination, monkeypatch)

    for index in range(4):
        destination.emit({"event": index}, log_type="event", severity="INFO")

    kept = [payload["event"] for payload, _, _ in destination._buffer]
    assert kept == [2, 3]
    overflow = [
        fields
        for operation, fields in failures
        if operation == "logging.destination_queue_overflow"
    ]
    assert len(overflow) == 1
    assert overflow[0]["dropped_total"] == 1
    assert destination._dropped == 2


def test_background_trips_after_consecutive_batch_failures(
    monkeypatch,
) -> None:
    import pytest

    destination, failures = _background(
        FlakyDestination(), batch_size=1, failure_limit=3
    )
    _inline(destination, monkeypatch)

    for index in range(5):
        destination.emit({"event": index}, log_type="event", severity="INFO")
    while not destination._tripped.is_set():
        destination._drain_once()

    counts = [
        fields["consecutive_failures"]
        for operation, fields in failures
        if operation == "logging.destination_emit_async"
    ]
    assert counts == [1, 2, 3]
    with pytest.raises(RuntimeError, match="tripped"):
        destination.emit({"event": "x"}, log_type="event", severity="INFO")


def test_background_trip_flows_through_manager_breaker(monkeypatch) -> None:
    from policyengine_observability.destinations.manager import (
        DESTINATION_FAILURE_LIMIT,
    )
    from policyengine_observability.destinations.stdout import (
        StdoutJsonDestination,
    )

    destination, _failures = _background(
        FlakyDestination(), batch_size=1, failure_limit=1
    )
    _inline(destination, monkeypatch)
    manager, manager_failures = _manager([destination])

    manager.emit({"event": "seed"}, log_type="event", severity="INFO")
    destination._drain_once()
    for _ in range(DESTINATION_FAILURE_LIMIT):
        manager.emit({"event": "x"}, log_type="event", severity="INFO")

    assert destination not in manager.destinations
    assert any(
        operation == "logging.destination_disabled"
        for operation, _fields in manager_failures
    )
    assert any(
        isinstance(existing, StdoutJsonDestination)
        for existing in manager.destinations
    )


def test_background_flush_drains_everything(monkeypatch) -> None:
    wrapped = BatchRecordingDestination()
    destination, failures = _background(wrapped, batch_size=2)
    _inline(destination, monkeypatch)

    for index in range(5):
        destination.emit({"event": index}, log_type="event", severity="INFO")
    destination.flush()

    assert sum(len(batch) for batch in wrapped.batches) == 5
    assert len(destination._buffer) == 0
    assert failures == []


def test_background_flush_reports_undelivered_remainder(
    monkeypatch,
) -> None:
    destination, failures = _background(
        FlakyDestination(), batch_size=1, failure_limit=1
    )
    _inline(destination, monkeypatch)

    for index in range(4):
        destination.emit({"event": index}, log_type="event", severity="INFO")
    destination.flush()

    incomplete = [
        fields
        for operation, fields in failures
        if operation == "logging.destination_flush_incomplete"
    ]
    assert len(incomplete) == 1
    assert incomplete[0]["remaining"] == 3


def test_background_restart_clears_trip_and_buffer(monkeypatch) -> None:
    wrapped = BatchRecordingDestination()
    destination, _failures = _background(
        FlakyDestination(), batch_size=1, failure_limit=1
    )
    _inline(destination, monkeypatch)
    destination.emit({"event": "x"}, log_type="event", severity="INFO")
    destination._drain_once()
    assert destination._tripped.is_set()

    destination.restart()
    destination.wrapped = wrapped
    _inline(destination, monkeypatch)

    assert not destination._tripped.is_set()
    assert len(destination._buffer) == 0
    destination.emit({"event": "y"}, log_type="event", severity="INFO")
    destination._drain_once()
    assert wrapped.batches == [[({"event": "y"}, "event", "INFO")]]


def test_background_worker_delivers_end_to_end() -> None:
    wrapped = BatchRecordingDestination()
    destination, failures = _background(
        wrapped, batch_size=10, batch_latency_seconds=0.01
    )

    for index in range(3):
        destination.emit({"event": index}, log_type="event", severity="INFO")
    destination.flush(1.0)
    assert destination._atexit_registered is True
    destination.close()

    assert sum(len(batch) for batch in wrapped.batches) == 3
    assert failures == []
    assert destination._atexit_registered is False


def test_background_worker_restarts_after_fork(monkeypatch) -> None:
    import os as os_module

    from policyengine_observability.destinations import background

    wrapped = BatchRecordingDestination()
    destination, _failures = _background(wrapped)
    destination.emit({"event": "a"}, log_type="event", severity="INFO")
    first_worker = destination._worker
    assert first_worker is not None

    real_pid = os_module.getpid()
    monkeypatch.setattr(background.os, "getpid", lambda: real_pid + 1)
    destination.emit({"event": "b"}, log_type="event", severity="INFO")

    assert destination._worker is not first_worker
    first_worker.join(timeout=2.0)
    assert not first_worker.is_alive()
    destination.flush(1.0)
    destination.close()


# ── Async wiring, lifecycle, and config knobs ────────────────────────────


def _config_manager(config):
    import json
    import logging

    from policyengine_observability.destinations.manager import (
        LogDestinationManager,
    )

    return LogDestinationManager(
        config=config,
        loggers={"event": logging.getLogger("test-wiring")},
        serializer=json.dumps,
        on_failure=lambda *args, **kwargs: None,
    )


def test_async_mode_wraps_google_destination(monkeypatch) -> None:
    from policyengine_observability.config import ObservabilityConfig
    from policyengine_observability.destinations import manager as manager_mod
    from policyengine_observability.destinations.background import (
        BackgroundEmitDestination,
    )

    class StubGoogle:
        name = "google_cloud_logging"

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def emit(self, payload, *, log_type, severity) -> None:
            pass

    monkeypatch.setattr(
        manager_mod, "GoogleCloudLoggingDestination", StubGoogle
    )
    manager = _config_manager(
        ObservabilityConfig(log_emit_mode="async", log_queue_size=7)
    )

    destination = manager._build_destination("google_cloud_logging")

    assert isinstance(destination, BackgroundEmitDestination)
    assert isinstance(destination.wrapped, StubGoogle)
    assert destination.queue_size == 7
    assert destination.name == "google_cloud_logging"


def test_sync_mode_returns_bare_destination(monkeypatch) -> None:
    from policyengine_observability.config import ObservabilityConfig
    from policyengine_observability.destinations import manager as manager_mod

    class StubGoogle:
        name = "google_cloud_logging"

        def __init__(self, **kwargs) -> None:
            pass

        def emit(self, payload, *, log_type, severity) -> None:
            pass

    monkeypatch.setattr(
        manager_mod, "GoogleCloudLoggingDestination", StubGoogle
    )
    manager = _config_manager(ObservabilityConfig())

    destination = manager._build_destination("google")

    assert isinstance(destination, StubGoogle)


def test_async_mode_never_wraps_stdout() -> None:
    from policyengine_observability.config import ObservabilityConfig
    from policyengine_observability.destinations.stdout import (
        StdoutJsonDestination,
    )

    manager = _config_manager(ObservabilityConfig(log_emit_mode="async"))

    destination = manager._build_destination("stdout")

    assert isinstance(destination, StdoutJsonDestination)


class FlushRecordingDestination:
    name = "flush-recording"

    def __init__(self) -> None:
        self.flushes = []
        self.restarts = 0

    def emit(self, payload, *, log_type, severity) -> None:
        pass

    def flush(self, deadline_seconds=None) -> None:
        self.flushes.append(deadline_seconds)

    def restart(self) -> None:
        self.restarts += 1


def test_manager_flush_reaches_capable_destinations() -> None:
    flushable = FlushRecordingDestination()
    plain = RecordingDestination()
    manager, failures = _manager([flushable, plain])

    manager.flush(1.5)

    assert flushable.flushes == [1.5]
    assert failures == []


def test_manager_restart_rebuilds_destinations_from_config() -> None:
    from policyengine_observability.destinations.stdout import (
        StdoutJsonDestination,
    )

    stale = FlushRecordingDestination()
    manager, _failures = _manager([stale])
    manager._consecutive_failures[id(stale)] = 2

    manager.restart()

    assert stale not in manager.destinations
    assert any(
        isinstance(destination, StdoutJsonDestination)
        for destination in manager.destinations
    )
    assert manager._consecutive_failures == {}


def test_manager_restart_revives_a_disabled_destination(monkeypatch) -> None:
    """The Modal snapshot-restore path: a destination tripped and
    disabled before the snapshot must come back after restart, with a
    fresh client."""
    from policyengine_observability.config import ObservabilityConfig
    from policyengine_observability.destinations import manager as manager_mod
    from policyengine_observability.destinations.manager import (
        DESTINATION_FAILURE_LIMIT,
    )

    built = []

    class StubGoogleRebuild:
        name = "google_cloud_logging"

        def __init__(self, **kwargs) -> None:
            built.append(self)

        def emit(self, payload, *, log_type, severity) -> None:
            raise RuntimeError("sink down")

    monkeypatch.setattr(
        manager_mod, "GoogleCloudLoggingDestination", StubGoogleRebuild
    )
    manager = _config_manager(
        ObservabilityConfig(log_destinations=("google_cloud_logging",))
    )
    manager.configure()
    for _ in range(DESTINATION_FAILURE_LIMIT):
        manager.emit({"event": "x"}, log_type="event", severity="INFO")
    assert built[0] not in manager.destinations

    manager.restart()

    assert len(built) == 2
    assert built[1] in manager.destinations


def test_manager_configure_closes_replaced_destinations() -> None:
    class ClosableDestination(FlushRecordingDestination):
        def __init__(self) -> None:
            super().__init__()
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    stale = ClosableDestination()
    manager, _failures = _manager([stale])

    manager.configure()

    assert stale.closed == 1
    assert stale not in manager.destinations


def test_background_flush_waits_for_in_flight_batch(monkeypatch) -> None:
    wrapped = BatchRecordingDestination()
    destination, failures = _background(wrapped)
    _inline(destination, monkeypatch)

    with destination._lock:
        destination._in_flight = 1
    destination.flush(0.05)

    incomplete = [
        fields
        for operation, fields in failures
        if operation == "logging.destination_flush_incomplete"
    ]
    assert len(incomplete) == 1
    assert incomplete[0]["remaining"] == 1
    with destination._lock:
        destination._in_flight = 0


def test_google_destination_reports_unbounded_transport(monkeypatch) -> None:
    failures = []
    monkeypatch.setattr(
        google_cloud_logging,
        "load_google_credentials",
        lambda *, prefer_workload_identity: None,
    )
    monkeypatch.setattr(
        google_cloud_logging,
        "configure_google_application_credentials",
        lambda: None,
    )
    GoogleCloudLoggingDestination(
        project=None,
        log_name="policyengine-observability",
        client_factory=lambda _project, _credentials: FakeClient(gapic=False),
        on_failure=lambda operation, exc, **fields: failures.append(
            (operation, fields)
        ),
    )

    assert failures[0][0] == "logging.destination_unbounded_transport"


def test_google_destination_stamps_event_timestamp(monkeypatch) -> None:
    client = FakeClient(gapic=False)
    destination = _destination(monkeypatch, client)

    destination.emit(
        {"created_at": "2026-07-08T00:00:00+00:00", "event": "x"},
        log_type="event",
        severity="INFO",
    )
    destination.emit(
        {"created_at": "not-a-timestamp", "event": "y"},
        log_type="event",
        severity="INFO",
    )

    (stamped,), _ = client.logging_api.calls[0]
    assert stamped["timestamp"] == "2026-07-08T00:00:00+00:00"
    (unstamped,), _ = client.logging_api.calls[1]
    assert "timestamp" not in unstamped


def test_internal_error_flag_is_thread_local() -> None:
    import threading

    from policyengine_observability import (
        ObservabilityConfig,
        ObservabilityRuntime,
    )

    runtime = ObservabilityRuntime(
        ObservabilityConfig(service_name="svc", otel_enabled=False)
    )
    runtime._emitting_internal_error = True
    seen_in_thread = []

    def read_flag() -> None:
        seen_in_thread.append(runtime._emitting_internal_error)

    thread = threading.Thread(target=read_flag)
    thread.start()
    thread.join()

    assert runtime._emitting_internal_error is True
    assert seen_in_thread == [False]


def test_manager_flush_reports_but_survives_failures() -> None:
    class ExplodingFlush(FlushRecordingDestination):
        def flush(self, deadline_seconds=None) -> None:
            raise RuntimeError("flush failed")

    manager, failures = _manager([ExplodingFlush()])

    manager.flush()

    assert any(
        operation == "logging.destination_flush"
        for operation, _fields in failures
    )


def test_runtime_shutdown_flushes_log_destinations() -> None:
    from policyengine_observability import (
        ObservabilityConfig,
        ObservabilityRuntime,
    )

    runtime = ObservabilityRuntime(
        ObservabilityConfig(service_name="svc", otel_enabled=False)
    )
    flushable = FlushRecordingDestination()
    runtime.log_destination_manager.destinations = [flushable]
    runtime.log_destination_manager.configured = True

    runtime.shutdown()

    assert flushable.flushes == [None]


def test_public_flush_and_restart_facades(monkeypatch) -> None:
    import policyengine_observability as observability

    calls = []

    class StubRuntime:
        def flush_log_destinations(self, deadline_seconds=None) -> None:
            calls.append(("flush", deadline_seconds))

        def restart_log_destinations(self) -> None:
            calls.append(("restart", None))

    monkeypatch.setattr(
        observability, "observability_runtime", lambda: StubRuntime()
    )

    observability.flush_observability(2.0)
    observability.restart_observability()

    assert calls == [("flush", 2.0), ("restart", None)]
    assert "flush_observability" in observability.__all__
    assert "restart_observability" in observability.__all__


def test_from_env_parses_emission_knobs(monkeypatch) -> None:
    from policyengine_observability.config import ObservabilityConfig

    monkeypatch.setenv("OBSERVABILITY_LOG_EMIT_MODE", "Async")
    monkeypatch.setenv("OBSERVABILITY_STDOUT_FORMAT", "Google")
    monkeypatch.setenv("OBSERVABILITY_LOG_QUEUE_SIZE", "50")
    monkeypatch.setenv("OBSERVABILITY_LOG_BATCH_SIZE", "5")
    monkeypatch.setenv("OBSERVABILITY_LOG_BATCH_LATENCY_SECONDS", "0.5")
    monkeypatch.setenv("OBSERVABILITY_LOG_FLUSH_DEADLINE_SECONDS", "9")
    monkeypatch.setenv("OBSERVABILITY_GOOGLE_LOG_TIMEOUT_SECONDS", "1.5")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.log_emit_mode == "async"
    assert config.stdout_format == "google"
    assert config.log_queue_size == 50
    assert config.log_batch_size == 5
    assert config.log_batch_latency_seconds == 0.5
    assert config.log_flush_deadline_seconds == 9.0
    assert config.google_log_timeout_seconds == 1.5


def test_from_env_emission_knobs_default_and_reject_garbage(
    monkeypatch,
) -> None:
    from policyengine_observability.config import ObservabilityConfig

    monkeypatch.delenv("OBSERVABILITY_LOG_EMIT_MODE", raising=False)
    monkeypatch.setenv("OBSERVABILITY_LOG_QUEUE_SIZE", "not-a-number")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.log_emit_mode == "sync"
    assert config.log_queue_size == 1000
