from __future__ import annotations

import builtins
import time
from types import SimpleNamespace

import pytest
from runtime_helpers import (
    AttributeFailingSpan,
    ExceptionFailingSpan,
    RecordingMeter,
    RecordingPropagator,
    RecordingTracer,
    ValidContextSpan,
    runtime,
)

from policyengine_observability import (
    RequestObservabilityContext,
)
from policyengine_observability import _state as state_module
from policyengine_observability import runtime as runtime_module


def test_shutdown_logs_provider_failures_and_timeout() -> None:
    class FailingProvider:
        def shutdown(self) -> None:
            raise RuntimeError("shutdown failed")

    class SlowProvider:
        def shutdown(self) -> None:
            time.sleep(0.05)

    observed = runtime(shutdown_timeout_seconds=0.001)
    observed.tracer_provider = FailingProvider()
    observed.meter_provider = SlowProvider()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )

    observed.shutdown()

    assert "otel.trace_shutdown" in failures
    assert "otel.shutdown_timeout" in failures


def test_shutdown_closes_destinations_with_full_budget_and_no_thread(
    monkeypatch,
) -> None:
    observed = runtime(shutdown_timeout_seconds=2.0)
    close_calls = []
    monkeypatch.setattr(
        observed.log_destination_manager,
        "close",
        lambda deadline=None: close_calls.append(deadline),
    )

    def fail_thread(*args, **kwargs):
        raise AssertionError(
            "no watchdog thread should exist without providers"
        )

    monkeypatch.setattr(runtime_module.threading, "Thread", fail_thread)

    observed.shutdown()

    assert close_calls == [2.0]


def test_shutdown_destination_deadline_fits_inside_provider_budget(
    monkeypatch,
) -> None:
    """The prior design gave the log flush a deadline larger than the
    join bounding it, starving provider shutdown; the deadline must be
    derived from (and smaller than) the shutdown budget."""

    class Provider:
        def __init__(self) -> None:
            self.shutdown_called = False

        def shutdown(self) -> None:
            self.shutdown_called = True

    observed = runtime(shutdown_timeout_seconds=2.0)
    provider = Provider()
    observed.tracer_provider = provider
    close_calls = []
    monkeypatch.setattr(
        observed.log_destination_manager,
        "close",
        lambda deadline=None: close_calls.append(deadline),
    )

    observed.shutdown()

    assert close_calls == [1.0]
    assert provider.shutdown_called


def test_shutdown_slow_destination_close_still_runs_providers() -> None:
    class Provider:
        def __init__(self) -> None:
            self.shutdown_called = False

        def shutdown(self) -> None:
            self.shutdown_called = True

    observed = runtime(shutdown_timeout_seconds=0.2)
    provider = Provider()
    observed.tracer_provider = provider
    observed.log_destination_manager.close = lambda deadline=None: time.sleep(
        0.05
    )

    observed.shutdown()

    assert provider.shutdown_called


def test_shutdown_clamps_pathological_budget(monkeypatch) -> None:
    observed = runtime(shutdown_timeout_seconds=float("inf"))
    close_calls = []
    monkeypatch.setattr(
        observed.log_destination_manager,
        "close",
        lambda deadline=None: close_calls.append(deadline),
    )

    observed.shutdown()

    assert close_calls == [3.0]


def test_restart_log_destinations_rebuilds_from_config() -> None:
    observed = runtime(otel_enabled=False)
    observed.configure()
    first = observed.log_destination_manager.destinations[0]

    observed.restart_log_destinations()

    rebuilt = observed.log_destination_manager.destinations
    assert len(rebuilt) == 1
    assert rebuilt[0] is not first
    assert observed.log_destination_manager.configured is True


def test_restart_log_destinations_noops_when_disabled(monkeypatch) -> None:
    """The kill switch must hold across forks and snapshot restores:
    a disabled runtime's restart must not build destinations."""
    observed = runtime(enabled=False)
    configure_calls = []
    monkeypatch.setattr(
        observed.log_destination_manager,
        "configure",
        lambda: configure_calls.append(True),
    )

    observed.restart_log_destinations()

    assert configure_calls == []


def test_shutdown_survives_destination_close_failure(monkeypatch) -> None:
    class Provider:
        def __init__(self) -> None:
            self.shutdown_called = False

        def shutdown(self) -> None:
            self.shutdown_called = True

    observed = runtime(shutdown_timeout_seconds=1.0)
    provider = Provider()
    observed.tracer_provider = provider
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )

    def broken_close(deadline=None):
        raise RuntimeError("close exploded")

    monkeypatch.setattr(
        observed.log_destination_manager, "close", broken_close
    )

    observed.shutdown()

    assert provider.shutdown_called
    assert "logging.destination_close" in failures


def test_configure_otel_creates_real_providers_and_instruments() -> None:
    observed = runtime(otel_enabled=True)

    observed.configure()

    assert observed.tracer is not None
    assert observed.meter is not None
    assert observed.trace is not None
    assert observed.propagate is not None


def test_configure_otel_with_exporters_does_not_throw() -> None:
    observed = runtime(
        otel_enabled=True,
        otlp_endpoint="http://localhost:4318",
        otlp_protocol="http/protobuf",
    )
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )

    observed.configure()

    assert observed.tracer_provider is not None
    assert observed.meter_provider is not None


def test_configure_otel_import_failure_is_logged(monkeypatch) -> None:
    observed = runtime(otel_enabled=True)
    failures = []
    original_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "opentelemetry":
            raise RuntimeError("otel missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )

    observed.configure()

    assert failures == ["otel.configure_imports"]


def test_configure_instruments_and_instrument_failures() -> None:
    observed = runtime()
    meter = RecordingMeter()
    observed.meter = meter

    observed._configure_instruments()

    assert len(meter.created) == 11

    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append((operation, fields.get("instrument")))
    )
    noop = observed._instrument(
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("factory failed")
        ),
        "broken",
    )

    noop.add(1)
    noop.record(1)
    assert failures == [("metrics.create_instrument", "broken")]


def test_request_span_lifecycle_records_enter_and_exit_failures() -> None:
    observed = runtime()
    observed.tracer = RecordingTracer(fail_enter=True)
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/span",
        path="/span",
        endpoint="span",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    observed._start_request_span(context)
    assert context.server_span is None

    observed.tracer = RecordingTracer(fail_exit=True)
    observed._start_request_span(context)
    observed._close_request_span(context, RuntimeError("failed"))
    observed._close_request_span(context, None)

    assert failures == ["otel.request_span_enter", "otel.request_span_exit"]


def test_safe_span_records_exception_and_preserves_user_error() -> None:
    observed = runtime()
    observed.tracer = RecordingTracer()

    with pytest.raises(RuntimeError, match="business failed"):
        with observed._safe_span("safe", {}):
            raise RuntimeError("business failed")

    assert isinstance(observed.tracer.span.exceptions[0], RuntimeError)


def test_span_and_segment_failure_helpers_are_logged(monkeypatch) -> None:
    observed = runtime()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    observed.trace = SimpleNamespace(
        get_current_span=lambda: AttributeFailingSpan()
    )
    observed._set_current_span_attributes({"key": "value"})

    observed.trace = SimpleNamespace(
        get_current_span=lambda: ExceptionFailingSpan()
    )
    observed._record_exception_on_span(
        ExceptionFailingSpan(),
        RuntimeError("failed"),
        handled=False,
        status_code=500,
    )
    observed._add_span_event("event", {"safe": "yes", "unsafe": object()})
    observed._record_segment_safely("missing_start", None, {})
    monkeypatch.setattr(
        observed,
        "_safe_perf_counter",
        lambda _operation: None,
    )
    observed._record_segment_safely("missing_end", 1.0, {})

    assert "otel.set_span_attributes" in failures
    assert "otel.record_exception" in failures


def test_segment_helpers_cover_operation_attrs_and_span_prefix() -> None:
    observed = runtime(span_prefix="svc")
    with observed.operation("job", flavor="cli"):
        attrs = observed._segment_span_attributes({"tool": "loader"})

    assert attrs["policyengine.operation"] == "job"
    assert attrs["tool"] == "loader"
    assert observed._span_name("load") == "svc.load"


def test_contextvar_failure_paths_are_logged(monkeypatch) -> None:
    class BrokenVar:
        def get(self):
            raise RuntimeError("get failed")

    observed = runtime()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    monkeypatch.setattr(state_module, "_REQUEST_CONTEXT", BrokenVar())
    monkeypatch.setattr(state_module, "_OPERATION_CONTEXT", BrokenVar())

    assert observed.current_context() is None
    assert observed.current_operation() is None
    assert failures == ["context.current", "operation.current"]


def test_runtime_owned_httpx_instrumentation_failure_does_not_throw(
    monkeypatch,
) -> None:
    observed = runtime(otel_enabled=True)
    failures = []
    original_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "opentelemetry.instrumentation.httpx":
            raise RuntimeError("instrumentation failed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    monkeypatch.setattr(
        observed,
        "log_observability_failure",
        lambda operation, exc, **fields: failures.append(operation),
    )

    observed.instrument_httpx()

    assert failures == ["httpx.auto_instrument"]


def test_runtime_owned_httpx_instrumentation_success_and_wrapper() -> None:
    from policyengine_observability.integrations.httpx import (
        instrument_httpx,
    )

    observed = runtime(otel_enabled=True)

    instrument_httpx(observed)
    instrument_httpx(observed)

    assert observed._httpx_instrumented is True


def test_traceparent_capture_and_valid_trace_ids() -> None:
    observed = runtime()
    propagator = RecordingPropagator()
    observed.propagate = propagator
    span = ValidContextSpan()
    observed.trace = SimpleNamespace(get_current_span=lambda: span)

    trace_id, span_id = observed._trace_ids()

    assert observed.traceparent_header().startswith("00-4bf92f")
    assert observed._extract_context({"traceparent": "parent"}) == {
        "parent": {"traceparent": "parent"}
    }
    assert propagator.extracted == {"traceparent": "parent"}
    assert trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert span_id == "00f067aa0ba902b7"
