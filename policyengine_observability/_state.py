"""Shared context variables for requests, operations, and segments."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

from .context import (
    OperationObservabilityContext,
    RequestObservabilityContext,
    SegmentTimingNode,
)

if TYPE_CHECKING:
    from .runtime import ObservabilityRuntime

OBSERVABILITY_INTERNAL_DISPATCH_HEADER = "X-PolicyEngine-Internal-Dispatch"
REQUEST_ID_HEADER = "X-PolicyEngine-Request-Id"
TRACEPARENT_HEADER = "traceparent"

_REQUEST_CONTEXT: ContextVar[RequestObservabilityContext | None] = ContextVar(
    "policyengine_request_observability_context",
    default=None,
)
_OPERATION_CONTEXT: ContextVar[OperationObservabilityContext | None] = (
    ContextVar(
        "policyengine_operation_observability_context",
        default=None,
    )
)
_TIMINGS: ContextVar[dict[str, float] | None] = ContextVar(
    "policyengine_observability_timings",
    default=None,
)
_TURN_START: ContextVar[float | None] = ContextVar(
    "policyengine_observability_turn_start",
    default=None,
)
_SEGMENT_STACK: ContextVar[tuple[tuple[int, SegmentTimingNode], ...]] = (
    ContextVar(
        "policyengine_observability_segment_stack",
        default=(),
    )
)


class ContextState:
    def __init__(self, runtime: ObservabilityRuntime) -> None:
        self.runtime = runtime

    def current_context(self) -> RequestObservabilityContext | None:
        try:
            return _REQUEST_CONTEXT.get()
        except BaseException as exc:
            self.runtime.log_observability_failure("context.current", exc)
            return None

    def current_operation(
        self,
    ) -> OperationObservabilityContext | None:
        try:
            return _OPERATION_CONTEXT.get()
        except BaseException as exc:
            self.runtime.log_observability_failure("operation.current", exc)
            return None
