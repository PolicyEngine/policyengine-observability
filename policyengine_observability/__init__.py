from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

from .config import ObservabilityConfig
from .context import RequestObservabilityContext
from .runtime import OBSERVABILITY_INTERNAL_DISPATCH_HEADER
from .runtime import REQUEST_ID_HEADER
from .runtime import TRACEPARENT_HEADER
from .runtime import ObservabilityRuntime
from .runtime import observability_runtime
from .runtime import set_observability_runtime
from .segments import UNKNOWN_SEGMENT
from .segments import coerce_segment_name


def current_context() -> RequestObservabilityContext | None:
    return observability_runtime().current_context()


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


def annotate(handle, **attrs: Any) -> None:
    observability_runtime().annotate(handle, **attrs)


def end_scope(handle, error: BaseException | None = None) -> None:
    observability_runtime().end_scope(handle, error)


def shutdown_tracing() -> None:
    observability_runtime().shutdown_tracing()


@contextmanager
def segment(name: Any, **attrs: Any) -> Iterator[Any]:
    with observability_runtime().segment(name, **attrs) as span:
        yield span


@asynccontextmanager
async def asegment(name: Any, **attrs: Any) -> AsyncIterator[Any]:
    async with observability_runtime().asegment(name, **attrs) as span:
        yield span


@contextmanager
def collect_timings(name: str = "operation", **attrs: Any):
    with observability_runtime().collect_timings(name, **attrs) as timings:
        yield timings


__all__ = [
    "OBSERVABILITY_INTERNAL_DISPATCH_HEADER",
    "REQUEST_ID_HEADER",
    "TRACEPARENT_HEADER",
    "UNKNOWN_SEGMENT",
    "ObservabilityConfig",
    "ObservabilityRuntime",
    "RequestObservabilityContext",
    "annotate",
    "asegment",
    "capture_context",
    "coerce_segment_name",
    "collect_timings",
    "current_context",
    "end_scope",
    "mark",
    "mark_ttft",
    "observability_runtime",
    "record_error",
    "record_event",
    "segment",
    "set_attribute",
    "set_observability_runtime",
    "shutdown_tracing",
    "start_scope",
    "traceparent_header",
]
