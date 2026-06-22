from __future__ import annotations

import asyncio
import builtins
from enum import StrEnum

import pytest

from policyengine_observability.config import DEFAULT_METRIC_ATTRIBUTE_KEYS
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


def test_standalone_segment_creates_implicit_operation_metrics() -> None:
    observed = runtime()
    observed.segment_duration = RecordingInstrument()
    observed.operation_duration = RecordingInstrument()
    observed.operations = RecordingInstrument()

    with observed.segment(SegmentName.LOAD, flavor="cli", tool="loader"):
        pass

    _, _, segment_attributes = observed.segment_duration.calls[0]
    _, _, operation_attributes = observed.operation_duration.calls[0]
    assert segment_attributes["operation"] == "load"
    assert segment_attributes["flavor"] == "cli"
    assert segment_attributes["tool"] == "loader"
    assert operation_attributes["operation"] == "load"
    assert operation_attributes["flavor"] == "cli"
    assert observed.current_operation() is None


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


def test_from_env_invalid_shutdown_timeout_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS", "bad")

    config = ObservabilityConfig.from_env(service_name="svc")

    assert config.shutdown_timeout_seconds == 3.0


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


def test_coerce_segment_name_validates_registry() -> None:
    assert coerce_segment_name(SegmentName.LOAD, registry=SegmentName) == (
        "load",
        True,
    )
    assert coerce_segment_name("other", registry=SegmentName) == (
        "other",
        False,
    )
