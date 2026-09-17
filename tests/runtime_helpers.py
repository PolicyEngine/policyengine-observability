from __future__ import annotations

from enum import StrEnum
from typing import Any

from policyengine_observability import (
    ObservabilityConfig,
    ObservabilityRuntime,
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
