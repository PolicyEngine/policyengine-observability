from __future__ import annotations

import json

from fakes import (
    ClosableRecordingDestination,
    FailingDestination,
    RecordingDestination,
    RecordingLogger,
    make_manager,
)

from policyengine_observability.config import ObservabilityConfig
from policyengine_observability.destinations.stdout import (
    StdoutJsonDestination,
    resolve_stdout_formatter,
)


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


def test_stdout_broken_formatter_factory_degrades_to_plain() -> None:
    from policyengine_observability.destinations.stdout import (
        _FORMATTER_FACTORIES,
        register_stdout_formatter,
    )

    def broken_factory(config):
        raise RuntimeError("factory bug")

    register_stdout_formatter("broken-test", broken_factory)
    try:
        failures = []
        formatter = resolve_stdout_formatter(
            ObservabilityConfig(stdout_format="broken-test"),
            on_failure=lambda operation, exc, **fields: failures.append(
                (operation, fields)
            ),
        )
    finally:
        _FORMATTER_FACTORIES.pop("broken_test", None)

    formatted = formatter({"event": "x"}, log_type="event", severity="INFO")

    assert formatted == {"event": "x"}
    assert len(failures) == 1
    assert failures[0][0] == "logging.stdout_format"
    assert failures[0][1]["stdout_format"] == "broken-test"


def test_configure_fallback_survives_broken_formatter_factory() -> None:
    """The crash path: no destination builds, so the manager's
    last-resort stdout fallback resolves the same broken formatter —
    configure must stay fail-open and emit must still write plain."""
    from policyengine_observability.destinations.stdout import (
        _FORMATTER_FACTORIES,
        register_stdout_formatter,
    )

    def broken_factory(config):
        raise RuntimeError("factory bug")

    register_stdout_formatter("broken-test", broken_factory)
    try:
        config = ObservabilityConfig(
            log_destinations=("nonexistent",), stdout_format="broken-test"
        )
        logger = RecordingLogger()
        manager, _failures = make_manager(config, loggers={"event": logger})

        manager.configure()  # raised before the resolver guard existed
        manager.emit({"event": "x"}, log_type="event", severity="INFO")
    finally:
        _FORMATTER_FACTORIES.pop("broken_test", None)

    assert _emitted_line(logger) == {"event": "x", "severity": "INFO"}


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
