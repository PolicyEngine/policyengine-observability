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
    assert retry is None
    assert timeout == 2.0
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
