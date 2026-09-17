"""Segment names, nested timings, and context managers."""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING, Any

from . import _state
from .context import (
    OperationObservabilityContext,
    RequestObservabilityContext,
    SegmentTimingNode,
    _metric_attrs,
)

if TYPE_CHECKING:
    from .runtime import ObservabilityRuntime

UNKNOWN_SEGMENT = "unknown_segment"


def segment_values(
    registry: type[Enum] | Iterable[str] | None,
) -> frozenset[str]:
    if registry is None:
        return frozenset()
    if isinstance(registry, type) and issubclass(registry, Enum):
        return frozenset(str(member.value) for member in registry)
    return frozenset(str(value) for value in registry)


def coerce_segment_name(
    value: Any,
    *,
    registry: type[Enum] | Iterable[str] | None = None,
) -> tuple[str, bool]:
    values = segment_values(registry)
    if isinstance(value, Enum):
        segment = str(value.value)
        return segment, not values or segment in values
    if isinstance(value, str):
        return value, not values or value in values
    try:
        segment = str(value)
    except BaseException:
        return UNKNOWN_SEGMENT, False
    return segment, False if values else True


MAX_SEGMENT_ATTR_LENGTH = 200
SENSITIVE_SEGMENT_ATTR_PARTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


class SegmentRecorder:
    def __init__(self, runtime: ObservabilityRuntime) -> None:
        self.runtime = runtime

    def segment(self, name: Any, **attrs: Any) -> Iterator[Any]:
        return _SegmentManager(self.runtime, name, attrs)

    @contextmanager
    def _segment_context(self, name: Any, **attrs: Any) -> Iterator[Any]:
        if not self.runtime.enabled:
            yield None
            return
        segment_name = self.runtime._coerce_segment(name)
        implicit_operation = self.runtime._start_implicit_operation(
            segment_name,
            attrs,
        )
        start = self.runtime._safe_perf_counter(
            f"segment.{segment_name}.start"
        )
        segment_tree_handle = self.runtime._start_segment_tree_node(
            segment_name,
            attrs,
        )
        span_attrs = self.runtime._segment_span_attributes(attrs)
        span_name = self.runtime._span_name(segment_name)
        error: BaseException | None = None
        with self.runtime._safe_span(span_name, span_attrs) as span:
            try:
                yield span
            except BaseException as exc:
                error = exc
                self.runtime._record_segment_safely(
                    segment_name,
                    start,
                    attrs,
                    segment_tree_handle=segment_tree_handle,
                )
                raise
            else:
                self.runtime._record_segment_safely(
                    segment_name,
                    start,
                    attrs,
                    segment_tree_handle=segment_tree_handle,
                )
            finally:
                self.runtime._reset_segment_tree_stack(segment_tree_handle)
                self.runtime.end_operation(implicit_operation, error)

    @asynccontextmanager
    async def asegment(self, name: Any, **attrs: Any) -> AsyncIterator[Any]:
        if not self.runtime.enabled:
            yield None
            return
        segment_name = self.runtime._coerce_segment(name)
        implicit_operation = self.runtime._start_implicit_operation(
            segment_name,
            attrs,
        )
        start = self.runtime._safe_perf_counter(
            f"segment.{segment_name}.start"
        )
        segment_tree_handle = self.runtime._start_segment_tree_node(
            segment_name,
            attrs,
        )
        span_attrs = self.runtime._segment_span_attributes(attrs)
        span_name = self.runtime._span_name(segment_name)
        error: BaseException | None = None
        with self.runtime._safe_span(span_name, span_attrs) as span:
            try:
                yield span
            except BaseException as exc:
                error = exc
                self.runtime._record_segment_safely(
                    segment_name,
                    start,
                    attrs,
                    segment_tree_handle=segment_tree_handle,
                )
                raise
            else:
                self.runtime._record_segment_safely(
                    segment_name,
                    start,
                    attrs,
                    segment_tree_handle=segment_tree_handle,
                )
            finally:
                self.runtime._reset_segment_tree_stack(segment_tree_handle)
                self.runtime.end_operation(implicit_operation, error)

    def _start_segment_tree_node(
        self,
        name: str,
        attrs: dict[str, Any],
    ) -> dict[str, Any] | None:
        try:
            owner = self.runtime._segment_tree_owner()
            if owner is None:
                return None
            owner.segment_sequence[0] += 1
            node = SegmentTimingNode(
                sequence=owner.segment_sequence[0],
                name=name,
                attrs=self.runtime._safe_segment_tree_attrs(attrs),
            )
            owner_id = id(owner.segment_tree)
            stack = _state._SEGMENT_STACK.get()
            if stack and stack[-1][0] == owner_id:
                stack[-1][1].children.append(node)
            else:
                owner.segment_tree.append(node)
            token = _state._SEGMENT_STACK.set((*stack, (owner_id, node)))
            return {"node": node, "token": token}
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "segment.tree_start",
                exc,
                segment=name,
            )
            return None

    def _finish_segment_tree_node(
        self,
        handle: dict[str, Any] | None,
        duration_seconds: float,
    ) -> None:
        if not handle:
            return
        try:
            node = handle.get("node")
            if not isinstance(node, SegmentTimingNode):
                return
            node.duration_ms = duration_seconds * 1000
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "segment.tree_finish",
                exc,
            )

    def _reset_segment_tree_stack(
        self,
        handle: dict[str, Any] | None,
    ) -> None:
        if not handle:
            return
        token = handle.get("token")
        if token is None:
            return
        try:
            _state._SEGMENT_STACK.reset(token)
        except BaseException as exc:
            self.runtime.log_observability_failure("segment.tree_reset", exc)

    def _segment_tree_owner(
        self,
    ) -> RequestObservabilityContext | OperationObservabilityContext | None:
        context = self.runtime.current_context()
        if context is not None:
            return context
        return self.runtime.current_operation()

    def _safe_segment_tree_attrs(
        self,
        attrs: dict[str, Any],
    ) -> dict[str, Any]:
        safe_attrs: dict[str, Any] = {}
        for key, value in attrs.items():
            key_text = self.runtime._safe_str(key)
            key_lower = key_text.lower()
            if any(part in key_lower for part in SENSITIVE_SEGMENT_ATTR_PARTS):
                continue
            if value is None:
                continue
            if hasattr(value, "value"):
                value = value.value
            if isinstance(value, bool | int | float):
                safe_attrs[key_text] = value
            elif isinstance(value, str):
                safe_attrs[key_text] = value[:MAX_SEGMENT_ATTR_LENGTH]
        return safe_attrs

    def _record_segment_flat_timing(
        self,
        context: RequestObservabilityContext | None,
        operation: OperationObservabilityContext | None,
        name: str,
        duration_ms: float,
    ) -> None:
        seen_timing_ids: set[int] = set()
        seen_count_ids: set[int] = set()
        for target in (context, operation):
            if target is None:
                continue
            timings_id = id(target.timings_ms)
            if timings_id not in seen_timing_ids:
                target.timings_ms[name] = round(
                    target.timings_ms.get(name, 0.0) + duration_ms,
                    3,
                )
                seen_timing_ids.add(timings_id)
            counts_id = id(target.timing_counts)
            if counts_id not in seen_count_ids:
                target.timing_counts[name] = (
                    target.timing_counts.get(name, 0) + 1
                )
                seen_count_ids.add(counts_id)

    def _record_segment_safely(
        self,
        name: str,
        start: float | None,
        attrs: dict[str, Any],
        *,
        segment_tree_handle: dict[str, Any] | None = None,
    ) -> None:
        if start is None:
            return
        end = self.runtime._safe_perf_counter(f"segment.{name}.end")
        if end is None:
            return
        try:
            duration = end - start
            self.runtime._finish_segment_tree_node(
                segment_tree_handle, duration
            )
            self.runtime._record_timing(name, duration)
            context = self.runtime.current_context()
            operation = self.runtime.current_operation()
            metric_extra = {
                key: value
                for key, value in attrs.items()
                if (
                    key in self.runtime.config.metric_attribute_keys
                    and value is not None
                )
            }
            duration_ms = duration * 1000
            self.runtime._record_segment_flat_timing(
                context,
                operation,
                name,
                duration_ms,
            )
            if operation is not None:
                metric_attributes = operation.metric_attributes(
                    segment=name,
                    **metric_extra,
                )
            elif context is not None:
                metric_attributes = context.metric_attributes(
                    segment=name,
                    **metric_extra,
                )
            else:
                metric_attributes = _metric_attrs(
                    {
                        "service.name": self.runtime.config.service_name,
                        "service.role": self.runtime.config.service_role,
                        "deployment.environment": self.runtime.config.environment,
                        "segment": name,
                        **metric_extra,
                    },
                    self.runtime.config.metric_attribute_keys,
                )
            self.runtime.record_segment_metric(
                name,
                duration,
                metric_attributes,
                backend_segment="backend" in metric_extra,
            )
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "request.record_segment",
                exc,
                segment=name,
            )

    def _record_timing(self, name: str, duration_seconds: float) -> None:
        try:
            timings = _state._TIMINGS.get()
            if timings is None:
                return
            key = f"{name}_ms"
            duration_ms = duration_seconds * 1000.0
            timings[key] = round(timings.get(key, 0.0) + duration_ms, 1)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "scope.record_timing",
                exc,
                segment=name,
            )

    def _coerce_segment(self, name: Any) -> str:
        segment, is_registered = coerce_segment_name(
            name,
            registry=self.runtime.segment_registry,
        )
        if not is_registered:
            self.runtime.log_observability_failure(
                "segment.coerce",
                ValueError("Unregistered observability segment."),
                segment=segment,
                segment_type=type(name).__name__,
            )
        return segment

    def _safe_perf_counter(self, operation: str) -> float | None:
        try:
            return time.perf_counter()
        except BaseException as exc:
            self.runtime.log_observability_failure(operation, exc)
            return None


class _SegmentManager:
    def __init__(
        self,
        runtime: ObservabilityRuntime,
        name: Any,
        attrs: dict[str, Any],
    ) -> None:
        self.runtime = runtime
        self.name = name
        self.attrs = attrs
        self.context_manager = None

    def __enter__(self):
        self.context_manager = self.runtime._segment_context(
            self.name,
            **self.attrs,
        )
        return self.context_manager.__enter__()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self.context_manager is None:
            return False
        return bool(self.context_manager.__exit__(exc_type, exc, traceback))

    async def __aenter__(self):
        self.context_manager = self.runtime.asegment(self.name, **self.attrs)
        return await self.context_manager.__aenter__()

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        if self.context_manager is None:
            return False
        return bool(
            await self.context_manager.__aexit__(exc_type, exc, traceback)
        )

    def __call__(self, func):
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                async with self.runtime.segment(self.name, **self.attrs):
                    return await func(*args, **kwargs)

            return async_wrapper

        @wraps(func)
        def wrapper(*args, **kwargs):
            with self.runtime.segment(self.name, **self.attrs):
                return func(*args, **kwargs)

        return wrapper
