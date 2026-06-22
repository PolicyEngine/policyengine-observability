from __future__ import annotations

import asyncio
from enum import StrEnum

import policyengine_observability as observability
from policyengine_observability import (
    ObservabilityConfig,
    ObservabilityRuntime,
)


class SegmentName(StrEnum):
    LOAD = "load"


class RecordingInstrument:
    def __init__(self) -> None:
        self.calls = []

    def add(self, value, attributes=None) -> None:
        self.calls.append(("add", value, attributes))

    def record(self, value, attributes=None) -> None:
        self.calls.append(("record", value, attributes))


def test_public_wrappers_delegate_to_configured_runtime() -> None:
    runtime = ObservabilityRuntime(
        ObservabilityConfig(service_name="svc"),
        segment_registry=SegmentName,
    )
    runtime.operation_duration = RecordingInstrument()
    runtime.segment_duration = RecordingInstrument()
    runtime.operations = RecordingInstrument()
    runtime.errors = RecordingInstrument()
    runtime.failover_events = RecordingInstrument()
    observability.set_observability_runtime(runtime)

    with observability.operation("job", flavor="cli"):
        observability.set_attribute("tool", "worker")
        observability.record_event("fallback_selected", reason="forced")
        observability.record_error(
            RuntimeError("handled"),
            handled=True,
            include_stack=False,
        )
        with observability.segment(SegmentName.LOAD, tool="loader"):
            observability.mark("custom_ms", 12.3)
            observability.mark_ttft()
        handle = observability.start_scope({}, name="nested")
        observability.annotate(handle, model="claude")
        observability.end_scope(handle)

    assert observability.current_context() is None
    assert observability.current_operation() is None
    assert observability.traceparent_header() is None
    assert observability.capture_context() is None
    assert runtime.operation_duration.calls
    assert runtime.segment_duration.calls
    assert runtime.errors.calls
    assert runtime.failover_events.calls


def test_public_decorators_support_sync_and_async_functions() -> None:
    runtime = ObservabilityRuntime(ObservabilityConfig(service_name="svc"))
    runtime.operation_duration = RecordingInstrument()
    runtime.segment_duration = RecordingInstrument()
    runtime.operations = RecordingInstrument()
    observability.set_observability_runtime(runtime)

    @observability.entrypoint("sync_job", flavor="cli")
    def sync_job() -> str:
        return "sync"

    @observability.segment("async_step", flavor="worker")
    async def async_step() -> str:
        return "async"

    assert sync_job() == "sync"
    assert asyncio.run(async_step()) == "async"
    observability.instrument_fastapi(object())
    observability.instrument_httpx()
    observability.shutdown_tracing()
    observability.shutdown_observability()

    assert runtime.operation_duration.calls
    assert runtime.segment_duration.calls


def test_public_async_segment_and_collect_timings_wrappers() -> None:
    runtime = ObservabilityRuntime(ObservabilityConfig(service_name="svc"))
    observability.set_observability_runtime(runtime)

    async def run() -> dict[str, float]:
        with observability.collect_timings("job") as timings:
            async with observability.asegment("load"):
                pass
        return timings

    assert "load_ms" in asyncio.run(run())
