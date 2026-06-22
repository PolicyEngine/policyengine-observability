from __future__ import annotations

import asyncio
from enum import StrEnum

import pytest

from policyengine_observability import UNKNOWN_SEGMENT
from policyengine_observability import ObservabilityConfig
from policyengine_observability import ObservabilityRuntime
from policyengine_observability import RequestObservabilityContext
from policyengine_observability import coerce_segment_name


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


class RecordingInstrument:
    def __init__(self) -> None:
        self.calls = []

    def add(self, value, attributes=None) -> None:
        self.calls.append(("add", value, attributes))

    def record(self, value, attributes=None) -> None:
        self.calls.append(("record", value, attributes))


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


def test_async_segment_records_timing() -> None:
    async def run() -> dict[str, float]:
        observed = runtime()
        with observed.collect_timings("request") as timings:
            async with observed.asegment(SegmentName.SAVE):
                pass
        return timings

    timings = asyncio.run(run())

    assert "save_ms" in timings


def test_segment_preserves_business_exception_and_records_timing() -> None:
    observed = runtime()

    with pytest.raises(ValueError, match="business failed"):
        with observed.collect_timings("request") as timings:
            with observed.segment(SegmentName.LOAD):
                raise ValueError("business failed")

    assert "load_ms" in timings


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


def test_collect_timings_records_block_exception_on_scope_span() -> None:
    observed = runtime()
    span = RecordingSpan()
    observed.tracer = RecordingTracer(span=span)

    with pytest.raises(RuntimeError, match="scope failed"):
        with observed.collect_timings("turn"):
            raise RuntimeError("scope failed")

    assert len(span.exceptions) == 1
    assert isinstance(span.exceptions[0], RuntimeError)


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
    assert observed.active_requests.calls[0][1] == 1
    assert observed.active_requests.calls[-1][1] == -1
    assert observed.requests.calls[0][1] == 1


def test_coerce_segment_name_validates_registry() -> None:
    assert coerce_segment_name(SegmentName.LOAD, registry=SegmentName) == (
        "load",
        True,
    )
    assert coerce_segment_name("other", registry=SegmentName) == (
        "other",
        False,
    )
