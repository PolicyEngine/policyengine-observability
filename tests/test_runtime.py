from __future__ import annotations

import asyncio
import builtins
import time
from enum import StrEnum
from types import SimpleNamespace
from typing import Any

import pytest

from policyengine_observability import (
    UNKNOWN_SEGMENT,
    ObservabilityConfig,
    ObservabilityRuntime,
    RequestObservabilityContext,
    coerce_segment_name,
)
from policyengine_observability import runtime as runtime_module
from policyengine_observability.config import DEFAULT_METRIC_ATTRIBUTE_KEYS
from policyengine_observability.destinations import (
    manager as destination_manager_module,
)


class SegmentName(StrEnum):
    LOAD = "load"
    SAVE = "save"


class RecordingSpan:
    def __init__(self) -> None:
        self.attributes = {}
        self.exceptions = []
        self.events = []
        self.status = None

    def set_attribute(self, key, value) -> None:
        self.attributes[key] = value

    def record_exception(self, exc) -> None:
        self.exceptions.append(exc)

    def set_status(self, status) -> None:
        self.status = status

    def add_event(self, event, fields) -> None:
        self.events.append((event, fields))

    def get_span_context(self):
        return type(
            "SpanContext",
            (),
            {"is_valid": False, "trace_id": 0, "span_id": 0},
        )()


class NamedRecordingSpan(RecordingSpan):
    def __init__(self) -> None:
        super().__init__()
        self.names = []

    def update_name(self, name: str) -> None:
        self.names.append(name)


class ValidContextSpan(RecordingSpan):
    def get_span_context(self):
        return type(
            "SpanContext",
            (),
            {
                "is_valid": True,
                "trace_id": 0x4BF92F3577B34DA6A3CE929D0E0E4736,
                "span_id": 0x00F067AA0BA902B7,
            },
        )()


class AttributeFailingSpan(RecordingSpan):
    def set_attribute(self, key, value) -> None:
        raise RuntimeError("attribute failed")


class ExceptionFailingSpan(RecordingSpan):
    def record_exception(self, exc) -> None:
        raise RuntimeError("record exception failed")


class RecordingSpanContextManager:
    def __init__(
        self,
        span: RecordingSpan,
        *,
        fail_exit: bool = False,
    ) -> None:
        self.span = span
        self.fail_exit = fail_exit
        self.exited = False

    def __enter__(self):
        return self.span

    def __exit__(self, *_args):
        self.exited = True
        if self.fail_exit:
            raise RuntimeError("span exit failed")
        return False


class RecordingTracer:
    def __init__(
        self,
        span: RecordingSpan | None = None,
        *,
        fail_enter: bool = False,
        fail_exit: bool = False,
    ) -> None:
        self.span = span or RecordingSpan()
        self.fail_enter = fail_enter
        self.fail_exit = fail_exit
        self.calls = []
        self.last_context_manager = None

    def start_as_current_span(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if self.fail_enter:
            raise RuntimeError("span enter failed")
        self.last_context_manager = RecordingSpanContextManager(
            self.span,
            fail_exit=self.fail_exit,
        )
        return self.last_context_manager


class RecordingMeter:
    def __init__(self) -> None:
        self.created = []

    def create_histogram(self, name, **kwargs):
        self.created.append(("histogram", name, kwargs))
        return RecordingInstrument()

    def create_counter(self, name, **kwargs):
        self.created.append(("counter", name, kwargs))
        return RecordingInstrument()

    def create_up_down_counter(self, name, **kwargs):
        self.created.append(("up_down_counter", name, kwargs))
        return RecordingInstrument()


class RecordingInstrument:
    def __init__(self) -> None:
        self.calls = []

    def add(self, value, attributes=None) -> None:
        self.calls.append(("add", value, attributes))

    def record(self, value, attributes=None) -> None:
        self.calls.append(("record", value, attributes))


class FailingInstrument:
    def add(self, *_args, **_kwargs) -> None:
        raise RuntimeError("metric failed")

    def record(self, *_args, **_kwargs) -> None:
        raise RuntimeError("metric failed")


class RecordingLogDestination:
    def __init__(self, name: str = "recording") -> None:
        self.name = name
        self.calls = []

    def emit(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> None:
        self.calls.append((payload, log_type, severity))


class FailingLogDestination:
    name = "failing"

    def emit(self, *_args, **_kwargs) -> None:
        raise RuntimeError("destination failed")


class RecordingPropagator:
    def __init__(self) -> None:
        self.extracted = None

    def inject(self, carrier) -> None:
        carrier["traceparent"] = (
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        )

    def extract(self, carrier):
        self.extracted = carrier
        return {"parent": carrier}


def runtime(**kwargs) -> ObservabilityRuntime:
    return ObservabilityRuntime(
        ObservabilityConfig(service_name="svc", **kwargs),
        segment_registry=SegmentName,
    )


def test_segment_records_aggregated_timing() -> None:
    observed = runtime()

    with observed.collect_timings("request") as timings:
        with observed.segment(SegmentName.LOAD):
            pass
        with observed.segment(SegmentName.LOAD):
            pass

    assert "load_ms" in timings
    assert timings["load_ms"] >= 0


def test_operation_log_accumulates_repeated_segment_timings() -> None:
    observed = runtime()
    handle = observed.start_operation("job")
    operation = handle["operation"]

    try:
        with observed.segment(SegmentName.LOAD):
            pass
        with observed.segment(SegmentName.LOAD):
            pass
    finally:
        observed.end_operation(handle)

    assert operation.timings_ms["load"] >= 0
    assert operation.timing_counts["load"] == 2
    payload = operation.as_log_record(trace_id=None, span_id=None)
    assert payload["timing_counts"]["load"] == 2


def test_operation_log_records_ordered_nested_segment_tree() -> None:
    observed = runtime()
    handle = observed.start_operation("job")
    operation = handle["operation"]

    try:
        with observed.segment(SegmentName.LOAD):
            with observed.segment(
                SegmentName.SAVE,
                simulation_kind="baseline",
                token="SECRET",
                payload={"not": "safe"},
            ):
                pass
            with observed.segment(
                SegmentName.SAVE,
                simulation_kind="reform",
            ):
                pass
    finally:
        observed.end_operation(handle)

    payload = operation.as_log_record(trace_id=None, span_id=None)
    tree = payload["segment_tree"]
    assert len(tree) == 1
    assert tree[0]["sequence"] == 1
    assert tree[0]["name"] == "load"
    assert "duration_ms" in tree[0]
    assert "self_ms" not in tree[0]

    children = tree[0]["children"]
    assert [child["sequence"] for child in children] == [2, 3]
    assert [child["name"] for child in children] == ["save", "save"]
    assert children[0]["attrs"] == {"simulation_kind": "baseline"}
    assert children[1]["attrs"] == {"simulation_kind": "reform"}
    assert "token" not in children[0].get("attrs", {})
    assert "payload" not in children[0].get("attrs", {})
    assert payload["timing_counts"]["save"] == 2


def test_operation_log_reserved_fields_override_attributes() -> None:
    observed = runtime()
    handle = observed.start_operation(
        "job",
        operation="attribute-operation",
        duration_ms="attribute-duration",
        timings_ms="attribute-timings",
        timing_counts="attribute-counts",
        segment_tree="attribute-tree",
        error="attribute-error",
    )
    operation = handle["operation"]

    try:
        with observed.segment(SegmentName.LOAD):
            pass
    finally:
        observed.end_operation(handle)

    payload = operation.as_log_record(trace_id=None, span_id=None)
    assert payload["operation"] == "job"
    assert isinstance(payload["duration_ms"], float)
    assert isinstance(payload["timings_ms"], dict)
    assert isinstance(payload["timing_counts"], dict)
    assert isinstance(payload["segment_tree"], list)
    assert payload["error"] is None


def test_async_segment_records_timing() -> None:
    async def run() -> dict[str, float]:
        observed = runtime()
        with observed.collect_timings("request") as timings:
            async with observed.asegment(SegmentName.SAVE):
                pass
        return timings

    timings = asyncio.run(run())

    assert "save_ms" in timings


def test_async_segments_keep_independent_segment_tree_stacks() -> None:
    async def run() -> list[dict[str, Any]]:
        observed = runtime()
        handle = observed.start_operation("job")
        operation = handle["operation"]

        async def branch(branch_name: str) -> None:
            async with observed.asegment(SegmentName.LOAD, branch=branch_name):
                await asyncio.sleep(0)
                async with observed.asegment(
                    SegmentName.SAVE,
                    branch=branch_name,
                ):
                    await asyncio.sleep(0)

        try:
            await asyncio.gather(branch("a"), branch("b"))
        finally:
            observed.end_operation(handle)
        return operation.as_log_record(trace_id=None, span_id=None)[
            "segment_tree"
        ]

    tree = asyncio.run(run())

    assert [node["name"] for node in tree] == ["load", "load"]
    assert [node["attrs"] for node in tree] == [
        {"branch": "a"},
        {"branch": "b"},
    ]
    assert [node["children"][0]["attrs"] for node in tree] == [
        {"branch": "a"},
        {"branch": "b"},
    ]


def test_segment_preserves_business_exception_and_records_timing() -> None:
    observed = runtime()

    with pytest.raises(ValueError, match="business failed"):
        with observed.collect_timings("request") as timings:
            with observed.segment(SegmentName.LOAD):
                raise ValueError("business failed")

    assert "load_ms" in timings


def test_segment_tree_records_failed_segments_before_reraising() -> None:
    observed = runtime()
    handle = observed.start_operation("job")
    operation = handle["operation"]
    error = None

    try:
        with observed.segment(SegmentName.LOAD):
            raise ValueError("business failed")
    except ValueError as exc:
        error = exc
    finally:
        observed.end_operation(handle, error)

    payload = operation.as_log_record(trace_id=None, span_id=None)
    assert payload["event"] == "operation_failed"
    assert payload["segment_tree"][0]["name"] == "load"
    assert "duration_ms" in payload["segment_tree"][0]


def test_unregistered_segment_falls_back_without_throwing() -> None:
    class BrokenString:
        def __str__(self) -> str:
            raise RuntimeError("cannot stringify")

    observed = runtime()

    with observed.collect_timings("request") as timings:
        with observed.segment(BrokenString()):
            pass

    assert f"{UNKNOWN_SEGMENT}_ms" in timings


def test_segment_span_start_failure_does_not_skip_user_code() -> None:
    observed = runtime()
    observed.tracer = RecordingTracer(fail_enter=True)
    executed = False

    with observed.collect_timings("request") as timings:
        with observed.segment(SegmentName.LOAD):
            executed = True

    assert executed
    assert "load_ms" in timings


def test_segment_span_exit_failure_does_not_escape() -> None:
    observed = runtime()
    observed.tracer = RecordingTracer(fail_exit=True)

    with observed.collect_timings("request") as timings:
        with observed.segment(SegmentName.LOAD):
            pass

    assert "load_ms" in timings


def test_disabled_runtime_noops_across_public_methods() -> None:
    observed = ObservabilityRuntime.disabled()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/disabled",
        path="/disabled",
        endpoint="disabled",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    handle = observed.start_operation("disabled")
    observed.end_operation(handle)
    observed.begin_request(context)
    observed.complete_request(200)
    observed.update_request_route(route="/other")
    observed.teardown_request(None)
    observed.set_attribute("key", "value")
    observed.record_error(RuntimeError("ignored"), handled=True)
    observed.record_event("ignored")

    with observed.segment(SegmentName.LOAD) as span:
        assert span is None

    async def run() -> None:
        async with observed.asegment(SegmentName.LOAD) as async_span:
            assert async_span is None

    asyncio.run(run())
    assert observed.prepare_response(200) == {}
    assert observed.current_context() is None
    assert observed.current_operation() is None


def test_span_attribute_failure_does_not_drop_span_lifecycle() -> None:
    observed = runtime()
    span = AttributeFailingSpan()
    observed.tracer = RecordingTracer(span=span)
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )

    with observed.segment(SegmentName.LOAD, tool="loader"):
        pass

    assert "otel.span_attributes" in failures
    assert observed.tracer.last_context_manager.exited


def test_collect_timings_records_block_exception_on_scope_span() -> None:
    observed = runtime()
    span = RecordingSpan()
    observed.tracer = RecordingTracer(span=span)

    with pytest.raises(RuntimeError, match="scope failed"):
        with observed.collect_timings("turn"):
            raise RuntimeError("scope failed")

    assert len(span.exceptions) == 1
    assert isinstance(span.exceptions[0], RuntimeError)


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

    monkeypatch.setattr(runtime_module, "_OPERATION_CONTEXT", BrokenVar())
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


def test_request_lifecycle_records_headers_and_context_metrics() -> None:
    observed = runtime()
    observed.active_requests = RecordingInstrument()
    observed.requests = RecordingInstrument()
    observed.http_duration = RecordingInstrument()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/calculate",
        path="/calculate",
        endpoint="calculate",
        query_keys=["country"],
        content_length_bytes=None,
        inbound={"ip_source": "remote_addr", "client_ip": "127.0.0.1"},
    )

    observed.begin_request(context)
    with observed.segment(SegmentName.LOAD):
        pass
    headers = observed.finish_request(200)
    observed.teardown_request(None)

    assert headers["X-PolicyEngine-Request-Id"] == "request-1"
    assert context.status_code == 200
    assert "load" in context.timings_ms
    assert observed.current_context() is None
    assert observed.current_operation() is None
    assert observed.active_requests.calls[0][1] == 1
    assert observed.active_requests.calls[-1][1] == -1
    assert observed.requests.calls[0][1] == 1


def test_request_log_accumulates_repeated_segment_timings() -> None:
    observed = runtime()
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

    observed.begin_request(context)
    with observed.segment(SegmentName.LOAD):
        pass
    with observed.segment(SegmentName.LOAD):
        pass
    observed.finish_request(200)
    observed.teardown_request(None)

    assert context.timings_ms["load"] >= 0
    assert context.timing_counts["load"] == 2
    payload = context.as_log_record(trace_id=None, span_id=None)
    assert payload["timing_counts"]["load"] == 2
    assert [node["name"] for node in payload["segment_tree"]] == [
        "load",
        "load",
    ]


def test_internal_dispatch_segments_merge_into_parent_operation() -> None:
    observed = runtime()
    handle = observed.start_operation(
        "modal_worker_dispatch",
        flavor="modal_worker",
    )
    parent_operation = handle["operation"]
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="POST",
        route="/calculate",
        path="/calculate",
        endpoint="calculate",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
        internal_dispatch=True,
    )

    try:
        observed.begin_request(context)
        with observed.segment(SegmentName.LOAD):
            pass
        observed.finish_request(200)
        observed.teardown_request(None)

        assert context.timings_ms is parent_operation.timings_ms
        assert context.timing_counts is parent_operation.timing_counts
        assert context.segment_tree is parent_operation.segment_tree
        assert "load" in parent_operation.timings_ms
        assert parent_operation.timing_counts["load"] == 1
        assert parent_operation.segment_tree[0].name == "load"
        assert observed.current_operation() is parent_operation
    finally:
        observed.end_operation(handle)

    assert observed.current_context() is None
    assert observed.current_operation() is None


def test_non_internal_request_timings_do_not_leak_to_parent_operation() -> (
    None
):
    observed = runtime()
    handle = observed.start_operation("job", flavor="worker")
    parent_operation = handle["operation"]
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="POST",
        route="/calculate",
        path="/calculate",
        endpoint="calculate",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    try:
        observed.begin_request(context)
        with observed.segment(SegmentName.LOAD):
            pass
        observed.finish_request(200)
        observed.teardown_request(None)

        assert context.timings_ms is not parent_operation.timings_ms
        assert context.segment_tree is not parent_operation.segment_tree
        assert "load" not in parent_operation.timings_ms
        assert parent_operation.segment_tree == []
        assert context.segment_tree[0].name == "load"
        assert observed.current_operation() is parent_operation
    finally:
        observed.end_operation(handle)

    assert observed.current_context() is None
    assert observed.current_operation() is None


def test_set_attribute_updates_explicit_operation_inside_request() -> None:
    observed = runtime()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/chat",
        path="/chat",
        endpoint="chat",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    observed.begin_request(context)
    handle = observed.start_operation("chat.turn", flavor="chat")
    operation = handle["operation"]
    try:
        observed.set_attribute("model", "claude")
    finally:
        observed.end_operation(handle)
        observed.teardown_request(None)

    assert context.attributes["model"] == "claude"
    assert context.operation_context.attributes["model"] == "claude"
    assert operation.attributes["model"] == "claude"
    assert observed.current_context() is None
    assert observed.current_operation() is None


def test_mark_ttft_attribute_updates_current_operation() -> None:
    observed = runtime()
    handle = observed.start_operation("chat.turn", flavor="chat")
    operation = handle["operation"]

    try:
        observed.mark_ttft_attribute()
    finally:
        observed.end_operation(handle)

    assert operation.attributes["ttft_ms"] >= 0


def test_request_methods_noop_without_current_context() -> None:
    observed = runtime()

    assert observed.prepare_response(200) == {}
    observed.complete_request(200)
    observed.update_request_route(route="/missing")
    observed.teardown_request(None)


def test_request_begin_operation_begin_and_lifecycle_failures_are_logged(
    monkeypatch,
) -> None:
    class BrokenVar:
        def set(self, _value):
            raise RuntimeError("set failed")

    observed = runtime()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/broken",
        path="/broken",
        endpoint="broken",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    monkeypatch.setattr(runtime_module, "_REQUEST_CONTEXT", BrokenVar())
    observed.begin_request(context)
    monkeypatch.setattr(
        runtime_module,
        "_REQUEST_CONTEXT",
        runtime_module.ContextVar("request", default=None),
    )
    monkeypatch.setattr(runtime_module, "_OPERATION_CONTEXT", BrokenVar())
    observed._begin_request_operation(context)

    assert failures == ["request.begin", "request.operation_begin"]


def test_request_prepare_complete_update_and_teardown_failures_are_logged() -> (
    None
):
    observed = runtime()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/broken",
        path="/broken",
        endpoint="broken",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )
    observed.begin_request(context)
    context.span_attributes = lambda **_extra: (_ for _ in ()).throw(
        RuntimeError("span attrs failed")
    )
    observed.prepare_response(200)
    observed.complete_request(200)
    observed.update_request_route(route="/other")
    observed.emit_request_log = lambda _context: (_ for _ in ()).throw(
        RuntimeError("emit failed")
    )
    observed.teardown_request(None)

    assert "request.prepare_response" in failures
    assert "request.complete" in failures
    assert "request.update_route" in failures
    assert "request.teardown" in failures


def test_set_attribute_failure_path_is_logged() -> None:
    observed = runtime()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    observed.current_context = lambda: SimpleNamespace(
        set_attribute=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("attribute failed")
        )
    )

    observed.set_attribute("tool", "loader")

    assert failures == ["request.set_attribute"]


def test_request_route_update_relabels_active_request_and_span() -> None:
    observed = runtime()
    observed.active_requests = RecordingInstrument()
    span = NamedRecordingSpan()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/initial",
        path="/items/1",
        endpoint="initial",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )
    context.server_span = span

    observed.begin_request(context)
    observed.update_request_route(route="/items/<id>", endpoint="item")
    observed.teardown_request(None)

    assert context.route == "/items/<id>"
    assert context.endpoint == "item"
    assert span.names == ["/items/<id>"]
    assert observed.active_requests.calls[1][1] == -1
    assert observed.active_requests.calls[2][1] == 1


def test_prepare_response_includes_traceparent_when_available() -> None:
    observed = runtime()
    observed.propagate = RecordingPropagator()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/trace",
        path="/trace",
        endpoint="trace",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    observed.begin_request(context)
    headers = observed.prepare_response(200)
    observed.teardown_request(None)

    assert headers["traceparent"].startswith("00-4bf92f")


def test_rate_limited_request_records_rate_limit_metric() -> None:
    observed = runtime()
    observed.rate_limited = RecordingInstrument()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/limited",
        path="/limited",
        endpoint="limited",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    observed.begin_request(context)
    headers = observed.finish_request(429)
    observed.teardown_request(None)

    assert headers["X-PolicyEngine-Request-Id"] == "request-1"
    assert context.attributes["rate_limited"] is True
    assert observed.rate_limited.calls[0][0] == "add"


def test_teardown_request_records_unhandled_exception() -> None:
    observed = runtime()
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
    observed.teardown_request(RuntimeError("failed"))

    assert context.status_code == 500
    assert context.error is not None
    assert context.error.handled is False
    assert observed.errors.calls[0][0] == "add"


def test_from_env_invalid_shutdown_timeout_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS", "bad")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.shutdown_timeout_seconds == 3.0


def test_from_env_enables_otel_by_default() -> None:
    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.otel_enabled is True


def test_from_env_allows_otel_opt_out(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_ENABLED", "false")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.otel_enabled is False


def test_from_env_ignores_legacy_observability_otel_switch(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OBSERVABILITY_OTEL_ENABLED", "false")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.otel_enabled is True


def test_from_env_reads_boolean_csv_and_environment(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_SERVICE_NAME", "env-svc")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "off")
    monkeypatch.setenv("OBSERVABILITY_REQUEST_LOGS_ENABLED", "false")
    monkeypatch.setenv("OBSERVABILITY_LOG_RAW_IP", "0")
    monkeypatch.setenv("OBSERVABILITY_LOG_LEVEL", "warning")
    monkeypatch.setenv("OTEL_ENABLED", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OBSERVABILITY_TRACER_NAME", "tracer")
    monkeypatch.setenv("OBSERVABILITY_METER_NAME", "meter")
    monkeypatch.setenv(
        "OBSERVABILITY_METRIC_ATTRIBUTE_KEYS",
        "service.name, custom",
    )
    monkeypatch.setenv(
        "OBSERVABILITY_EXTRA_METRIC_ATTRIBUTE_KEYS",
        "custom, other",
    )

    config = ObservabilityConfig.from_env(
        service_name="svc",
        instrument_fastapi=True,
        instrument_httpx=True,
    )

    assert config.service_name == "env-svc"
    assert config.environment == "production"
    assert config.enabled is False
    assert config.request_logs_enabled is False
    assert config.log_raw_ip is False
    assert config.otel_enabled is True
    assert config.otlp_endpoint == "http://collector"
    assert config.otlp_protocol == "http/protobuf"
    assert config.tracer_name == "tracer"
    assert config.meter_name == "meter"
    assert config.instrument_fastapi is True
    assert config.instrument_httpx is True
    assert config.metric_attribute_keys == ("service.name", "custom", "other")


def test_from_env_reads_log_destinations_and_google_config(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OBSERVABILITY_LOG_DESTINATIONS",
        "stdout, google-cloud-logging, stdout",
    )
    monkeypatch.setenv("GCP_PROJECT", "fallback-project")
    monkeypatch.setenv("OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME", "custom-log")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.log_destinations == ("stdout", "google-cloud-logging")
    assert config.google_cloud_project == "fallback-project"
    assert config.google_cloud_log_name == "custom-log"


def test_from_env_uses_default_log_destinations_without_env(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OBSERVABILITY_LOG_DESTINATIONS", raising=False)

    config = ObservabilityConfig.from_env(
        service_name="svc",
        default_log_destinations=("google_cloud_logging",),
    )

    assert config.log_destinations == ("google_cloud_logging",)


def test_from_env_log_destinations_env_overrides_default(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OBSERVABILITY_LOG_DESTINATIONS", "stdout")

    config = ObservabilityConfig.from_env(
        service_name="svc",
        default_log_destinations=("google_cloud_logging",),
    )

    assert config.log_destinations == ("stdout",)


def test_metric_attribute_keys_are_configurable() -> None:
    config = ObservabilityConfig(
        service_name="svc",
        metric_attribute_keys=("service.name", "tool"),
    )
    context = RequestObservabilityContext(
        config=config,
        request_id="request-1",
        method="POST",
        route="/chat",
        path="/chat",
        endpoint="chat",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )
    context.set_attribute("tool", "search")
    context.set_attribute("model", "claude")

    assert context.metric_attributes() == {
        "service.name": "svc",
        "tool": "search",
    }


def test_context_set_attribute_normalizes_enum_values() -> None:
    observed = runtime()
    operation = observed.start_operation("job")["operation"]
    request = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/",
        path="/",
        endpoint="root",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    operation.set_attribute("segment", SegmentName.LOAD)
    request.set_attribute("segment", SegmentName.SAVE)
    observed.end_operation({"operation": operation})

    assert operation.attributes["segment"] == "load"
    assert request.attributes["segment"] == "save"


def test_metric_attribute_keys_can_be_extended_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_EXTRA_METRIC_ATTRIBUTE_KEYS", "custom")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.metric_attribute_keys == (
        *DEFAULT_METRIC_ATTRIBUTE_KEYS,
        "custom",
    )


def test_segment_metric_uses_configured_metric_attribute_keys() -> None:
    observed = runtime(
        metric_attribute_keys=(
            "service.name",
            "route",
            "method",
            "segment",
            "tool",
        )
    )
    observed.segment_duration = RecordingInstrument()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="POST",
        route="/chat",
        path="/chat",
        endpoint="chat",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    observed.begin_request(context)
    with observed.segment(SegmentName.LOAD, tool="search", model="claude"):
        pass
    observed.finish_request(200)
    observed.teardown_request(None)

    _, _, attributes = observed.segment_duration.calls[0]
    assert attributes["tool"] == "search"
    assert "model" not in attributes


def test_shutdown_calls_trace_and_metric_providers() -> None:
    class Provider:
        def __init__(self) -> None:
            self.shutdown_called = False

        def shutdown(self) -> None:
            self.shutdown_called = True

    observed = runtime(shutdown_timeout_seconds=1)
    trace_provider = Provider()
    meter_provider = Provider()
    observed.tracer_provider = trace_provider
    observed.meter_provider = meter_provider

    observed.shutdown()

    assert trace_provider.shutdown_called
    assert meter_provider.shutdown_called


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
    monkeypatch.setattr(runtime_module, "_REQUEST_CONTEXT", BrokenVar())
    monkeypatch.setattr(runtime_module, "_OPERATION_CONTEXT", BrokenVar())

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


def test_trace_helpers_log_failures_without_throwing() -> None:
    observed = runtime()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    observed.propagate = SimpleNamespace(
        inject=lambda _carrier: (_ for _ in ()).throw(RuntimeError("inject")),
        extract=lambda _carrier: (_ for _ in ()).throw(
            RuntimeError("extract")
        ),
    )
    observed.trace = SimpleNamespace(
        get_current_span=lambda: (_ for _ in ()).throw(RuntimeError("span"))
    )

    assert observed.traceparent_header() is None
    assert observed._extract_context({"traceparent": "parent"}) is None
    assert observed._current_span() is None
    assert failures == [
        "request.traceparent_header",
        "otel.extract_context",
        "otel.current_span",
    ]


def test_record_event_covers_operation_context_and_no_context_metrics() -> (
    None
):
    observed = runtime()
    observed.failover_events = RecordingInstrument()

    with observed.operation("worker", flavor="queue"):
        observed.record_event("modal_retry", attempt=1, ignored=None)

    observed.record_event("fallback_without_context")

    assert len(observed.failover_events.calls) == 2
    assert observed.failover_events.calls[0][2]["operation"] == "worker"
    assert observed.failover_events.calls[1][2]["event"] == (
        "fallback_without_context"
    )


def test_record_event_request_context_and_emit_log_skip_paths() -> None:
    observed = runtime()
    observed.failover_events = RecordingInstrument()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/event",
        path="/event",
        endpoint="event",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
        internal_dispatch=True,
    )
    observed.begin_request(context)
    observed.record_event("fallback_request", detail="request")
    observed.emit_request_log(context)
    observed.emit_request_log(context)
    observed.teardown_request(None)

    operation = observed.start_operation("job")["operation"]
    observed.emit_operation_log(operation)
    observed.emit_operation_log(operation)
    observed.end_operation({"operation": operation})

    assert observed.failover_events.calls[0][2]["route"] == "/event"
    assert context.emitted is True
    assert operation.emitted is True


def test_operation_log_emits_to_configured_destinations_once() -> None:
    observed = runtime()
    destination = RecordingLogDestination()
    observed.log_destination_manager.destinations = [destination]
    observed.log_destination_manager.configured = True

    handle = observed.start_operation("job")
    operation = handle["operation"]
    observed.end_operation(handle)
    observed.emit_operation_log(operation)

    assert len(destination.calls) == 1
    payload, log_type, severity = destination.calls[0]
    assert payload["operation"] == "job"
    assert payload["severity"] == "INFO"
    assert log_type == "operation"
    assert severity == "INFO"


def test_request_log_emits_to_configured_destination_once() -> None:
    observed = runtime()
    destination = RecordingLogDestination()
    observed.log_destination_manager.destinations = [destination]
    observed.log_destination_manager.configured = True
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/calculate",
        path="/calculate",
        endpoint="calculate",
        query_keys=[],
        content_length_bytes=None,
        inbound={"client_ip": "203.0.113.1"},
        status_code=200,
    )

    observed.emit_request_log(context)
    observed.emit_request_log(context)

    assert len(destination.calls) == 1
    payload, log_type, severity = destination.calls[0]
    assert payload["request_id"] == "request-1"
    assert payload["client_ip"] == "203.0.113.1"
    assert payload["severity"] == "INFO"
    assert log_type == "request"
    assert severity == "INFO"


def test_request_log_reserved_fields_override_inbound_and_attributes() -> None:
    observed = runtime()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/calculate",
        path="/calculate",
        endpoint="calculate",
        query_keys=[],
        content_length_bytes=None,
        inbound={
            "request_id": "inbound-request",
            "status_code": "inbound-status",
            "duration_ms": "inbound-duration",
            "segment_tree": "inbound-tree",
        },
        attributes={
            "request_id": "attribute-request",
            "status_code": "attribute-status",
            "duration_ms": "attribute-duration",
            "timings_ms": "attribute-timings",
            "timing_counts": "attribute-counts",
            "segment_tree": "attribute-tree",
            "error": "attribute-error",
        },
        status_code=204,
    )

    payload = context.as_log_record(trace_id=None, span_id=None)
    assert payload["request_id"] == "request-1"
    assert payload["status_code"] == 204
    assert isinstance(payload["duration_ms"], float)
    assert isinstance(payload["timings_ms"], dict)
    assert isinstance(payload["timing_counts"], dict)
    assert payload["segment_tree"] == []
    assert payload["error"] is None


def test_event_log_emits_to_configured_destination() -> None:
    observed = runtime()
    destination = RecordingLogDestination()
    observed.log_destination_manager.destinations = [destination]
    observed.log_destination_manager.configured = True

    observed.record_event("custom_event", detail="value")

    assert len(destination.calls) == 1
    payload, log_type, severity = destination.calls[0]
    assert payload["event"] == "custom_event"
    assert payload["detail"] == "value"
    assert payload["service_name"] == "svc"
    assert payload["severity"] == "INFO"
    assert log_type == "event"
    assert severity == "INFO"


def test_log_severity_maps_status_codes_and_errors() -> None:
    observed = runtime()

    assert observed._severity_for_log_record({"status_code": 200}) == "INFO"
    assert observed._severity_for_log_record({"status_code": 404}) == (
        "WARNING"
    )
    assert observed._severity_for_log_record({"status_code": "500"}) == (
        "ERROR"
    )
    assert (
        observed._severity_for_log_record({"error": {"handled": False}})
        == "ERROR"
    )
    assert observed._severity_for_log_record({"error": {"handled": True}}) == (
        "WARNING"
    )


def test_destination_failure_logs_internal_error_without_throwing() -> None:
    observed = runtime()
    recording = RecordingLogDestination()
    observed.log_destination_manager.destinations = [
        FailingLogDestination(),
        recording,
    ]
    observed.log_destination_manager.configured = True

    with observed.operation("job"):
        pass

    assert any(
        payload["event"] == "observability_internal_error"
        and payload["operation"] == "logging.destination_emit"
        for payload, _log_type, _severity in recording.calls
    )
    assert any(
        payload.get("operation") == "job"
        for payload, _log_type, _severity in recording.calls
    )


def test_all_destination_failures_fall_back_to_stderr(capsys) -> None:
    observed = runtime()
    observed.log_destination_manager.destinations = [FailingLogDestination()]
    observed.log_destination_manager.configured = True

    with observed.operation("job"):
        pass

    stderr = capsys.readouterr().err
    assert "observability_internal_error" in stderr
    assert "logging.destination_emit" in stderr


def test_unknown_destination_falls_back_to_stdout() -> None:
    observed = ObservabilityRuntime(
        ObservabilityConfig(
            service_name="svc",
            otel_enabled=False,
            log_destinations=("missing",),
        )
    )

    observed.configure()

    assert [
        destination.name
        for destination in observed.log_destination_manager.destinations
    ] == ["stdout"]


def test_google_destination_init_failure_falls_back_to_stdout(
    monkeypatch,
) -> None:
    def fail_google_destination(**_kwargs):
        raise ImportError("google-cloud-logging missing")

    monkeypatch.setattr(
        destination_manager_module,
        "GoogleCloudLoggingDestination",
        fail_google_destination,
    )
    observed = ObservabilityRuntime(
        ObservabilityConfig(
            service_name="svc",
            otel_enabled=False,
            log_destinations=("google_cloud_logging",),
        )
    )

    observed.configure()

    assert [
        destination.name
        for destination in observed.log_destination_manager.destinations
    ] == ["stdout"]


def test_disabled_configure_does_not_initialize_log_destinations(
    monkeypatch,
) -> None:
    def fail_google_destination(**_kwargs):
        raise AssertionError("google destination should not initialize")

    monkeypatch.setattr(
        destination_manager_module,
        "GoogleCloudLoggingDestination",
        fail_google_destination,
    )
    observed = ObservabilityRuntime(
        ObservabilityConfig(
            service_name="svc",
            enabled=False,
            log_destinations=("google_cloud_logging",),
        )
    )

    observed.configure()

    assert observed.log_destination_manager.destinations == []
    assert observed.log_destination_manager.configured is False


def test_record_segment_metric_covers_calculation_and_backend() -> None:
    observed = runtime()
    observed.segment_duration = RecordingInstrument()
    observed.calculate_duration = RecordingInstrument()
    observed.backend_duration = RecordingInstrument()

    observed.record_segment_metric(
        "calculation",
        0.1,
        {"backend": "modal"},
        backend_segment=True,
    )

    assert observed.segment_duration.calls
    assert observed.calculate_duration.calls
    assert observed.backend_duration.calls


def test_metric_recording_failures_are_logged() -> None:
    observed = runtime()
    observed.operation_duration = FailingInstrument()
    observed.http_duration = FailingInstrument()
    observed.segment_duration = FailingInstrument()
    observed.errors = FailingInstrument()
    observed.rate_limited = FailingInstrument()
    observed.failover_events = FailingInstrument()
    observed.active_requests = FailingInstrument()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )

    observed.record_operation_metric(0.1, {})
    observed.record_request_metric(0.1, {})
    observed.record_segment_metric("load", 0.1, {})
    observed.record_error_metric({})
    observed.record_rate_limited_metric({})
    observed.record_failover_event_metric({})
    observed.record_active_request(1, {})

    assert failures == [
        "metrics.record_operation",
        "metrics.record_request",
        "metrics.record_segment",
        "metrics.record_error",
        "metrics.record_rate_limited",
        "metrics.record_failover_event",
        "metrics.add_active_request",
    ]


def test_private_safety_helpers_cover_fallback_paths(
    monkeypatch,
    capsys,
) -> None:
    observed = runtime()

    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("cannot stringify")

    assert observed._safe_str(Unprintable()) == "<unprintable Unprintable>"
    assert observed._safe_traceback(Unprintable()) == ""

    original_dumps = __import__("json").dumps

    def failing_dumps(payload, *args, **kwargs):
        if payload.get("event") == "bad":
            raise RuntimeError("json failed")
        return original_dumps(payload, *args, **kwargs)

    monkeypatch.setattr("json.dumps", failing_dumps)
    assert "observability_internal_error" in observed._json({"event": "bad"})

    monkeypatch.setattr(
        "policyengine_observability.runtime.INTERNAL_LOGGER.error",
        lambda _message: (_ for _ in ()).throw(RuntimeError("logger failed")),
    )
    observed.log_observability_failure("test", RuntimeError("failed"))
    assert "observability_internal_error" in capsys.readouterr().err


def test_coerce_segment_name_validates_registry() -> None:
    assert coerce_segment_name(SegmentName.LOAD, registry=SegmentName) == (
        "load",
        True,
    )
    assert coerce_segment_name("other", registry=SegmentName) == (
        "other",
        False,
    )
    assert coerce_segment_name("load", registry=["load"]) == ("load", True)
    assert coerce_segment_name(SegmentName.LOAD, registry=None) == (
        "load",
        True,
    )
