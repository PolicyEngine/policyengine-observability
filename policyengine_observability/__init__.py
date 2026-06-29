from __future__ import annotations

from typing import Any

from .config import ObservabilityConfig
from .context import OperationObservabilityContext, RequestObservabilityContext
from .google_credentials import (
    configure_google_application_credentials,
    load_google_credentials,
)
from .runtime import (
    OBSERVABILITY_INTERNAL_DISPATCH_HEADER,
    REQUEST_ID_HEADER,
    TRACEPARENT_HEADER,
    ObservabilityRuntime,
    observability_runtime,
    set_observability_runtime,
)
from .segments import UNKNOWN_SEGMENT, coerce_segment_name


def current_context() -> RequestObservabilityContext | None:
    return observability_runtime().current_context()


def current_operation() -> OperationObservabilityContext | None:
    return observability_runtime().current_operation()


def set_attribute(key: str, value: Any) -> None:
    observability_runtime().set_attribute(key, value)


def record_error(
    exc: BaseException,
    *,
    handled: bool,
    status_code: int | None = None,
    include_stack: bool = True,
) -> None:
    observability_runtime().record_error(
        exc,
        handled=handled,
        status_code=status_code,
        include_stack=include_stack,
    )


def record_event(event: str, **fields: Any) -> None:
    observability_runtime().record_event(event, **fields)


def traceparent_header() -> str | None:
    return observability_runtime().traceparent_header()


def capture_context():
    return observability_runtime().capture_context()


def mark(key: str, ms: float) -> None:
    observability_runtime().mark(key, ms)


def mark_ttft(key: str = "ttft_ms") -> None:
    observability_runtime().mark_ttft(key)


def mark_ttft_attribute(key: str = "ttft_ms") -> None:
    observability_runtime().mark_ttft_attribute(key)


def start_scope(
    timings: dict[str, float],
    *,
    name: str = "operation",
    parent_context: Any = None,
    **attrs: Any,
):
    return observability_runtime().start_scope(
        timings,
        name=name,
        parent_context=parent_context,
        **attrs,
    )


def annotate(handle=None, **attrs: Any) -> None:
    observability_runtime().annotate(handle, **attrs)


def end_scope(handle, error: BaseException | None = None) -> None:
    observability_runtime().end_scope(handle, error)


def instrument_fastapi(app: Any) -> None:
    observability_runtime().instrument_fastapi(app)


def instrument_httpx() -> None:
    observability_runtime().instrument_httpx()


def shutdown_observability() -> None:
    observability_runtime().shutdown()


def shutdown_tracing() -> None:
    shutdown_observability()


def operation(name: str, *, flavor: str | None = None, **attrs: Any):
    return observability_runtime().operation(name, flavor=flavor, **attrs)


def entrypoint(
    name: str | None = None,
    *,
    flavor: str | None = None,
    **attrs: Any,
):
    return observability_runtime().entrypoint(
        name,
        flavor=flavor,
        **attrs,
    )


def segment(name: Any, **attrs: Any):
    return observability_runtime().segment(name, **attrs)


def asegment(name: Any, **attrs: Any):
    return observability_runtime().asegment(name, **attrs)


def collect_timings(name: str = "operation", **attrs: Any):
    return observability_runtime().collect_timings(name, **attrs)


__all__ = [
    "OBSERVABILITY_INTERNAL_DISPATCH_HEADER",
    "REQUEST_ID_HEADER",
    "TRACEPARENT_HEADER",
    "UNKNOWN_SEGMENT",
    "OperationObservabilityContext",
    "ObservabilityConfig",
    "ObservabilityRuntime",
    "RequestObservabilityContext",
    "annotate",
    "asegment",
    "capture_context",
    "coerce_segment_name",
    "collect_timings",
    "configure_google_application_credentials",
    "load_google_credentials",
    "current_context",
    "current_operation",
    "end_scope",
    "entrypoint",
    "instrument_fastapi",
    "instrument_httpx",
    "mark",
    "mark_ttft",
    "mark_ttft_attribute",
    "observability_runtime",
    "operation",
    "record_error",
    "record_event",
    "segment",
    "set_attribute",
    "set_observability_runtime",
    "shutdown_observability",
    "shutdown_tracing",
    "start_scope",
    "traceparent_header",
]
