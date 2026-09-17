from __future__ import annotations

import asyncio
from typing import Any

import pytest
from runtime_helpers import (
    AttributeFailingSpan,
    RecordingSpan,
    RecordingTracer,
    SegmentName,
    runtime,
)

from policyengine_observability import (
    UNKNOWN_SEGMENT,
    ObservabilityRuntime,
    RequestObservabilityContext,
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
