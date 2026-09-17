from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from runtime_helpers import (
    RecordingInstrument,
    RecordingSpan,
    RecordingTracer,
    SegmentName,
    runtime,
)

from policyengine_observability import (
    RequestObservabilityContext,
)
from policyengine_observability import _state as state_module


def test_operation_context_manager_async_and_exception_paths() -> None:
    async def run() -> None:
        observed = runtime()
        observed.errors = RecordingInstrument()

        with pytest.raises(RuntimeError, match="async failed"):
            async with observed.operation("async_job", flavor="worker"):
                raise RuntimeError("async failed")

        assert observed.errors.calls[0][2]["error_type"] == "RuntimeError"
        assert observed.current_operation() is None

    asyncio.run(run())


def test_start_operation_with_parent_context_attaches_and_detaches() -> None:
    observed = runtime()
    observed.tracer = RecordingTracer()
    parent_context = object()

    handle = observed.start_operation(
        "parented",
        parent_context=parent_context,
    )
    observed.end_operation(handle)

    assert observed.tracer.calls[0][0] == "parented"
    assert observed.current_operation() is None


def test_operation_attach_detach_and_reset_failures_are_logged(
    monkeypatch,
) -> None:
    from opentelemetry import context as otel_context

    observed = runtime()
    observed.tracer = RecordingTracer()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append((operation, fields.get("token")))
    )
    monkeypatch.setattr(
        otel_context,
        "attach",
        lambda _context: (_ for _ in ()).throw(RuntimeError("attach failed")),
    )

    handle = observed.start_operation("job", parent_context=object())
    observed.end_operation(handle)

    monkeypatch.setattr(
        otel_context,
        "detach",
        lambda _token: (_ for _ in ()).throw(RuntimeError("detach failed")),
    )
    observed.end_operation(
        {
            "operation": None,
            "context_token": object(),
            "timings_token": object(),
            "start_token": object(),
            "operation_token": object(),
        }
    )

    assert ("operation.context_attach", None) in failures
    assert ("operation.context_detach", None) in failures
    assert ("operation.context_reset", "timings_token") in failures
    assert ("operation.context_reset", "start_token") in failures
    assert ("operation.context_reset", "operation_token") in failures


def test_operation_end_and_start_failures_are_logged(monkeypatch) -> None:
    class BrokenVar:
        def set(self, _value):
            raise RuntimeError("set failed")

    observed = runtime()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    operation_handle = observed.start_operation("job")
    operation = operation_handle["operation"]
    operation.metric_recorded = True
    observed.complete_operation(operation)
    observed.complete_operation = lambda _operation: (_ for _ in ()).throw(
        RuntimeError("complete failed")
    )
    observed.end_operation(operation_handle)

    monkeypatch.setattr(state_module, "_OPERATION_CONTEXT", BrokenVar())
    handle = observed.start_operation("job")

    assert handle["operation"] is None
    assert failures == ["operation.end", "operation.start"]


def test_standalone_segment_creates_implicit_operation_metrics() -> None:
    observed = runtime()
    observed.segment_duration = RecordingInstrument()
    observed.operation_duration = RecordingInstrument()
    observed.operations = RecordingInstrument()
    emitted_payloads = []
    observed.emit_operation_log = lambda operation: emitted_payloads.append(
        operation.as_log_record(trace_id=None, span_id=None)
    )

    with observed.segment(SegmentName.LOAD, flavor="cli", tool="loader"):
        pass

    _, _, segment_attributes = observed.segment_duration.calls[0]
    _, _, operation_attributes = observed.operation_duration.calls[0]
    assert segment_attributes["operation"] == "load"
    assert segment_attributes["flavor"] == "cli"
    assert segment_attributes["tool"] == "loader"
    assert operation_attributes["operation"] == "load"
    assert operation_attributes["flavor"] == "cli"
    assert emitted_payloads[0]["segment_tree"][0]["name"] == "load"
    assert emitted_payloads[0]["segment_tree"][0]["attrs"] == {
        "flavor": "cli",
        "tool": "loader",
    }
    assert observed.current_operation() is None


def test_segment_with_request_context_does_not_create_implicit_operation(
    monkeypatch,
) -> None:
    observed = runtime()
    observed.segment_duration = RecordingInstrument()
    observed.operation_duration = RecordingInstrument()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/calculate",
        path="/calculate",
        endpoint="calculate",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )
    monkeypatch.setattr(observed, "current_context", lambda: context)

    with observed.segment(SegmentName.LOAD):
        pass

    _, _, attributes = observed.segment_duration.calls[0]
    assert attributes["route"] == "/calculate"
    assert observed.operation_duration.calls == []


def test_start_scope_outside_request_records_operation_segment_metrics() -> (
    None
):
    observed = runtime()
    observed.segment_duration = RecordingInstrument()
    timings: dict[str, float] = {}

    handle = observed.start_scope(
        timings,
        name="chat_turn",
        flavor="chat",
        model="claude",
    )
    with observed.segment(SegmentName.LOAD, tool="search"):
        pass
    observed.end_scope(handle)

    _, _, attributes = observed.segment_duration.calls[0]
    assert "load_ms" in timings
    assert attributes["operation"] == "chat_turn"
    assert attributes["flavor"] == "chat"
    assert attributes["model"] == "claude"
    assert attributes["tool"] == "search"


def test_nested_scope_annotates_span_context_and_operation() -> None:
    observed = runtime()
    observed.tracer = RecordingTracer()
    parent_context = object()
    timings: dict[str, float] = {}

    with observed.operation("outer", flavor="chat"):
        handle = observed.start_scope(
            timings,
            name="inner",
            parent_context=parent_context,
        )
        observed.annotate(handle, model="claude")
        observed.mark("custom_ms", 1.23)
        observed.mark_ttft()
        observed.end_scope(handle)

    span = observed.tracer.span
    assert span.attributes["model"] == "claude"
    assert "custom_ms" in timings


def test_entrypoint_decorator_records_operation_metrics() -> None:
    observed = runtime()
    observed.operation_duration = RecordingInstrument()
    observed.operations = RecordingInstrument()

    @observed.entrypoint("import_data", flavor="cli")
    def run_import() -> str:
        return "done"

    assert run_import() == "done"
    _, _, attributes = observed.operation_duration.calls[0]
    assert attributes["operation"] == "import_data"
    assert attributes["flavor"] == "cli"


def test_async_segment_decorator_records_segment_metrics() -> None:
    observed = runtime()
    observed.segment_duration = RecordingInstrument()

    @observed.segment(SegmentName.SAVE, flavor="worker")
    async def save() -> str:
        return "saved"

    assert asyncio.run(save()) == "saved"
    _, _, attributes = observed.segment_duration.calls[0]
    assert attributes["operation"] == "save"
    assert attributes["flavor"] == "worker"


def test_record_error_outside_request_uses_operation_context() -> None:
    observed = runtime()
    observed.errors = RecordingInstrument()

    with observed.operation("worker", flavor="queue"):
        observed.record_error(
            RuntimeError("failed"),
            handled=True,
            include_stack=False,
        )

    _, _, attributes = observed.errors.calls[0]
    assert attributes["operation"] == "worker"
    assert attributes["flavor"] == "queue"
    assert attributes["error_type"] == "RuntimeError"


def test_record_error_on_request_updates_span_status() -> None:
    observed = runtime()
    span = RecordingSpan()
    observed.trace = SimpleNamespace(get_current_span=lambda: span)
    observed.StatusCode = SimpleNamespace(ERROR="ERROR")
    observed.Status = lambda code, message: (code, message)
    observed.errors = RecordingInstrument()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/error",
        path="/error",
        endpoint="error",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    observed.begin_request(context)
    observed.record_error(
        RuntimeError("failed"),
        handled=True,
        status_code=500,
    )
    observed.teardown_request(None)

    assert span.exceptions
    assert span.status == ("ERROR", "failed")
