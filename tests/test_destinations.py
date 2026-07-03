from __future__ import annotations

from policyengine_observability.destinations import (
    GoogleCloudLoggingDestination,
    google_cloud_logging,
    normalize_payload,
)


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("cannot stringify")


class FakeLogger:
    def __init__(self) -> None:
        self.calls = []

    def log_struct(self, payload, **kwargs) -> None:
        self.calls.append((payload, kwargs))


class FakeClient:
    def __init__(self) -> None:
        self.project = "resolved-project"
        self.fake_logger = FakeLogger()
        self.log_names = []

    def logger(self, log_name: str) -> FakeLogger:
        self.log_names.append(log_name)
        return self.fake_logger


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


def test_google_destination_writes_structured_log_with_bounded_labels(
    monkeypatch,
) -> None:
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
    client = FakeClient()
    destination = GoogleCloudLoggingDestination(
        project=None,
        log_name="policyengine-observability",
        client_factory=lambda _project, _credentials: client,
    )

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

    payload, kwargs = client.fake_logger.calls[0]
    assert client.log_names == ["policyengine-observability"]
    assert payload["object"].startswith("<object object at ")
    assert kwargs["severity"] == "ERROR"
    assert kwargs["trace"] == "projects/resolved-project/traces/abc123"
    assert kwargs["span_id"] == "def456"
    assert kwargs["labels"] == {
        "log_type": "request",
        "service_name": "svc",
        "service_role": "api",
        "environment": "production",
        "schema_version": "policyengine.observability.request.v1",
    }
    assert "request_id" not in kwargs["labels"]
    assert "path" not in kwargs["labels"]


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
