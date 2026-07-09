from __future__ import annotations

import json
import math

import pytest
from fakes import (
    ClosableRecordingDestination,
    FailingDestination,
    RecordingDestination,
    RecordingLogger,
    make_manager,
)

from policyengine_observability.config import ObservabilityConfig
from policyengine_observability.destinations import (
    GoogleCloudLoggingDestination,
    google_cloud_logging,
    normalize_payload,
)
from policyengine_observability.destinations.base import (
    accepts_keyword,
    clamped,
)
from policyengine_observability.destinations.stdout import (
    StdoutJsonDestination,
    resolve_stdout_formatter,
)


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("cannot stringify")


class FakeLogger:
    def __init__(self) -> None:
        self.calls = []

    def log_struct(self, payload, **kwargs) -> None:
        self.calls.append((payload, kwargs))


class FakeGapicApi:
    def __init__(self) -> None:
        self.calls = []

    def write_log_entries(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


class FakeLoggingApi:
    def __init__(self) -> None:
        self._gapic_api = FakeGapicApi()


class FakeClient:
    def __init__(self, *, gapic: bool = False) -> None:
        self.project = "resolved-project"
        self.fake_logger = FakeLogger()
        self.log_names = []
        if gapic:
            self.logging_api = FakeLoggingApi()

    def logger(self, log_name: str) -> FakeLogger:
        self.log_names.append(log_name)
        return self.fake_logger


def _google_destination(monkeypatch, client, **kwargs):
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5.0, 5.0),
        ("5", 5.0),
        (0, 0.5),
        (-3, 0.5),
        (1000, 60.0),
        (float("inf"), 10.0),
        (float("nan"), 10.0),
        (None, 10.0),
        ("garbage", 10.0),
    ],
)
def test_clamped_bounds_and_rejects_non_finite(value, expected) -> None:
    result = clamped(value, low=0.5, high=60.0, default=10.0)

    assert result == expected
    assert math.isfinite(result)


def test_accepts_keyword_covers_named_var_keyword_and_uninspectable() -> None:
    def named(payload, *, timestamp=None):
        pass

    def var_keyword(payload, **kwargs):
        pass

    def blind(payload):
        pass

    assert accepts_keyword(named, "timestamp") is True
    assert accepts_keyword(var_keyword, "timestamp") is True
    assert accepts_keyword(blind, "timestamp") is False
    # Builtins without introspectable signatures degrade to False
    # instead of raising at construction time.
    assert accepts_keyword(min, "timestamp") is False


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


def test_google_destination_bounds_gapic_writes(monkeypatch) -> None:
    client = FakeClient(gapic=True)

    destination = _google_destination(
        monkeypatch, client, write_timeout_seconds=5.0
    )
    # The write path under log_struct funnels through this method; the
    # rebinding must inject the bounded retry and per-call timeout.
    client.logging_api._gapic_api.write_log_entries(request="sentinel")

    assert destination.write_timeout_seconds == 5.0
    ((args, kwargs),) = client.logging_api._gapic_api.calls
    assert kwargs["request"] == "sentinel"
    assert kwargs["timeout"] == 5.0
    assert kwargs["retry"].timeout == 5.0


def test_google_destination_clamps_write_timeout(monkeypatch) -> None:
    destination = _google_destination(
        monkeypatch, FakeClient(gapic=True), write_timeout_seconds=0.0
    )

    assert destination.write_timeout_seconds == 0.5


def test_google_destination_without_gapic_transport_still_works(
    monkeypatch,
) -> None:
    client = FakeClient()

    destination = _google_destination(monkeypatch, client)
    destination.emit({"event": "x"}, log_type="event", severity="INFO")

    assert len(client.fake_logger.calls) == 1


def test_google_destination_forwards_enqueue_timestamp(monkeypatch) -> None:
    from datetime import UTC, datetime

    client = FakeClient()
    destination = _google_destination(monkeypatch, client)
    stamp = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)

    destination.emit(
        {"event": "x"}, log_type="event", severity="INFO", timestamp=stamp
    )
    destination.emit({"event": "y"}, log_type="event", severity="INFO")

    (_, stamped_kwargs), (_, plain_kwargs) = client.fake_logger.calls
    assert stamped_kwargs["timestamp"] is stamp
    assert "timestamp" not in plain_kwargs


def test_google_destination_close_closes_client(monkeypatch) -> None:
    class ClosableFakeClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    client = ClosableFakeClient()
    destination = _google_destination(monkeypatch, client)

    destination.close()

    assert client.closed == 1


def test_google_destination_close_tolerates_closeless_client(
    monkeypatch,
) -> None:
    destination = _google_destination(monkeypatch, FakeClient())

    destination.close()  # FakeClient has no close; must be a no-op


def test_google_destination_suppresses_instrumentation_entry(
    monkeypatch,
) -> None:
    logging_v2 = pytest.importorskip("google.cloud.logging_v2")
    monkeypatch.setattr(
        logging_v2, "_instrumentation_emitted", False, raising=False
    )

    _google_destination(monkeypatch, FakeClient())

    assert logging_v2._instrumentation_emitted is True


def test_google_factory_reads_write_timeout_env(monkeypatch) -> None:
    captured = {}

    class StubDestination:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        google_cloud_logging, "GoogleCloudLoggingDestination", StubDestination
    )
    monkeypatch.setenv("OBSERVABILITY_GOOGLE_WRITE_TIMEOUT_SECONDS", "2.5")

    from policyengine_observability.destinations.registry import (
        destination_strategy,
    )

    destination_strategy("google_cloud_logging").factory(
        config=ObservabilityConfig(google_cloud_project="proj"),
        loggers={},
        serializer=json.dumps,
    )

    assert captured["project"] == "proj"
    assert captured["write_timeout_seconds"] == 2.5


def test_google_factory_write_timeout_defaults_without_env(
    monkeypatch,
) -> None:
    captured = {}

    class StubDestination:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        google_cloud_logging, "GoogleCloudLoggingDestination", StubDestination
    )
    monkeypatch.delenv(
        "OBSERVABILITY_GOOGLE_WRITE_TIMEOUT_SECONDS", raising=False
    )

    from policyengine_observability.destinations.registry import (
        destination_strategy,
    )

    destination_strategy("google_cloud_logging").factory(
        config=ObservabilityConfig(google_cloud_project="proj"),
        loggers={},
        serializer=json.dumps,
    )

    assert captured["write_timeout_seconds"] == 10.0


# ── Stdout formatters ────────────────────────────────────────────────────


def _stdout_destination(config=None, formatter=None):
    logger = RecordingLogger()
    if formatter is None and config is not None:
        formatter = resolve_stdout_formatter(config)
    destination = StdoutJsonDestination(
        loggers={"event": logger},
        serializer=json.dumps,
        formatter=formatter,
    )
    return destination, logger


def _emitted_line(logger):
    ((_, message),) = logger.lines
    return json.loads(message)


def test_stdout_google_formatter_maps_agent_native_keys() -> None:
    config = ObservabilityConfig(
        stdout_format="google", google_cloud_project="proj"
    )
    destination, logger = _stdout_destination(config)

    destination.emit(
        {
            "schema_version": "policyengine.observability.event.v1",
            "service_name": "svc",
            "event": "x",
            "trace_id": "abc123",
            "span_id": 456,
        },
        log_type="event",
        severity="error",
    )

    line = _emitted_line(logger)
    assert line["severity"] == "ERROR"
    assert line["logging.googleapis.com/trace"] == (
        "projects/proj/traces/abc123"
    )
    assert line["logging.googleapis.com/spanId"] == "456"
    assert line["logging.googleapis.com/labels"] == {
        "log_type": "event",
        "service_name": "svc",
        "schema_version": "policyengine.observability.event.v1",
    }
    assert "time" not in line
    assert line["event"] == "x"


def test_stdout_google_formatter_omits_trace_without_project() -> None:
    config = ObservabilityConfig(stdout_format="google")
    destination, logger = _stdout_destination(config)

    destination.emit(
        {"event": "x", "trace_id": "abc"}, log_type="event", severity="INFO"
    )

    line = _emitted_line(logger)
    assert "logging.googleapis.com/trace" not in line


def test_stdout_unknown_format_falls_back_to_plain() -> None:
    config = ObservabilityConfig(stdout_format=" GoOgLeX ")
    destination, logger = _stdout_destination(config)

    destination.emit({"event": "x"}, log_type="event", severity="INFO")

    assert _emitted_line(logger) == {"event": "x"}


def test_stdout_unknown_format_reports_when_channel_available() -> None:
    failures = []
    formatter = resolve_stdout_formatter(
        ObservabilityConfig(stdout_format="agent-natve"),
        on_failure=lambda operation, exc, **fields: failures.append(
            (operation, str(exc))
        ),
    )

    formatted = formatter({"event": "x"}, log_type="event", severity="INFO")

    assert formatted == {"event": "x"}
    assert len(failures) == 1
    assert failures[0][0] == "logging.stdout_format"
    assert "agent-natve" in failures[0][1]


def test_stdout_format_name_is_normalized() -> None:
    config = ObservabilityConfig(stdout_format=" GOOGLE ")
    destination, logger = _stdout_destination(config)

    destination.emit({"event": "x"}, log_type="event", severity="INFO")

    assert _emitted_line(logger)["severity"] == "INFO"


def test_stdout_broken_formatter_degrades_to_unformatted() -> None:
    def broken(payload, *, log_type, severity):
        raise RuntimeError("formatter bug")

    destination, logger = _stdout_destination(formatter=broken)

    destination.emit({"event": "x"}, log_type="event", severity="INFO")

    assert _emitted_line(logger) == {"event": "x"}


def test_stdout_google_formatter_never_mutates_caller_payload() -> None:
    config = ObservabilityConfig(
        stdout_format="google", google_cloud_project="proj"
    )
    destination, logger = _stdout_destination(config)
    payload = {"event": "x", "trace_id": "abc"}

    destination.emit(payload, log_type="event", severity="INFO")

    assert payload == {"event": "x", "trace_id": "abc"}


def test_custom_stdout_formatter_registers_and_resolves() -> None:
    from policyengine_observability.destinations.stdout import (
        _FORMATTER_FACTORIES,
        register_stdout_formatter,
    )

    def factory(config):
        return lambda payload, *, log_type, severity: {"wrapped": payload}

    register_stdout_formatter("custom-test", factory)
    try:
        # Hyphen/underscore variance is forgiven the same way it is for
        # destination names.
        config = ObservabilityConfig(stdout_format=" Custom_Test ")
        destination, logger = _stdout_destination(config)
        destination.emit({"event": "x"}, log_type="event", severity="INFO")
    finally:
        _FORMATTER_FACTORIES.pop("custom_test", None)

    assert _emitted_line(logger) == {"wrapped": {"event": "x"}}


# ── Destination circuit breaker and manager lifecycle ───────────────────


def test_destination_disabled_after_consecutive_emit_failures() -> None:
    from policyengine_observability.destinations.manager import (
        DESTINATION_FAILURE_LIMIT,
    )

    flaky = FailingDestination()
    healthy = RecordingDestination()
    manager, failures = make_manager(destinations=[flaky, healthy])

    for _ in range(DESTINATION_FAILURE_LIMIT + 2):
        manager.emit({"event": "x"}, log_type="event", severity="INFO")

    assert flaky.calls == DESTINATION_FAILURE_LIMIT
    assert flaky not in manager.destinations
    assert len(healthy.payloads) == DESTINATION_FAILURE_LIMIT + 2
    assert any(op == "logging.destination_disabled" for op, *_ in failures)


def test_emit_success_resets_the_failure_counter() -> None:
    from policyengine_observability.destinations.manager import (
        DESTINATION_FAILURE_LIMIT,
    )

    flaky = FailingDestination(fail_first=DESTINATION_FAILURE_LIMIT - 1)
    manager, failures = make_manager(destinations=[flaky])

    for _ in range(DESTINATION_FAILURE_LIMIT + 2):
        manager.emit({"event": "x"}, log_type="event", severity="INFO")

    assert flaky in manager.destinations
    counts = [
        fields["consecutive_failures"]
        for op, _exc, fields in failures
        if op == "logging.destination_emit"
    ]
    assert max(counts) == DESTINATION_FAILURE_LIMIT - 1


def test_sole_disabled_destination_falls_back_to_stdout() -> None:
    from policyengine_observability.destinations.manager import (
        DESTINATION_FAILURE_LIMIT,
    )

    flaky = FailingDestination()
    manager, _failures = make_manager(destinations=[flaky])

    for _ in range(DESTINATION_FAILURE_LIMIT + 1):
        manager.emit({"event": "x"}, log_type="event", severity="INFO")

    assert flaky not in manager.destinations
    assert any(
        isinstance(destination, StdoutJsonDestination)
        for destination in manager.destinations
    )


def test_manager_close_reports_failures_and_closes_the_rest() -> None:
    class ExplodingClose(RecordingDestination):
        def close(self) -> None:
            raise RuntimeError("close failed")

    exploding = ExplodingClose()
    closable = ClosableRecordingDestination()
    manager, failures = make_manager(destinations=[exploding, closable])

    manager.close(1.0)  # must not raise

    assert closable.closed == 1
    assert any(op == "logging.destination_close" for op, *_ in failures)


def test_manager_close_accepts_zero_argument_close() -> None:
    """A duck-typed close(self) works under every close path — the
    deadline is passed only when the signature accepts it."""
    closable = ClosableRecordingDestination()
    manager, failures = make_manager(destinations=[closable])

    manager.close(1.0)

    assert closable.closed == 1
    assert failures == []


def test_manager_close_shares_one_deadline_across_destinations() -> None:
    import time

    deadlines = []

    class SlowClose(RecordingDestination):
        def close(self, deadline_seconds=None) -> None:
            deadlines.append(deadline_seconds)
            time.sleep(0.05)

    manager, _failures = make_manager(destinations=[SlowClose(), SlowClose()])

    manager.close(1.0)

    first, second = deadlines
    # The second destination only gets what the first one left, so N
    # stuck destinations cannot take N times the budget.
    assert first <= 1.0
    assert second <= first - 0.04


def test_emit_falls_back_to_stdout_when_configure_crashes(
    monkeypatch,
) -> None:
    logger = RecordingLogger()
    manager, failures = make_manager(loggers={"event": logger})

    def broken_configure() -> None:
        raise RuntimeError("configure exploded")

    monkeypatch.setattr(manager, "configure", broken_configure)

    manager.emit({"event": "x"}, log_type="event", severity="INFO")

    assert manager.configured is True
    assert isinstance(manager.destinations[0], StdoutJsonDestination)
    assert _emitted_line(logger) == {"event": "x", "severity": "INFO"}
    assert any(op == "logging.destination_config" for op, *_ in failures)


def test_reconfigure_closes_previous_after_installing_new() -> None:
    """Close-phase failure reports route through emit; the replaced
    destinations must be closed only after the new ones are installed
    so those reports still have a sink."""
    sink_states = []
    manager, _failures = make_manager()

    class CloseProbe(RecordingDestination):
        def close(self) -> None:
            sink_states.append(list(manager.destinations))

    manager.destinations = [CloseProbe()]
    manager.configured = True

    manager.configure()

    assert len(sink_states) == 1
    assert sink_states[0], "previous destination closed before new install"
