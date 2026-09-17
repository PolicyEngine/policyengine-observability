"""Operation lifecycle and timing-scope behavior."""

from __future__ import annotations

import inspect
import time
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any

from . import _state
from .context import (
    ErrorRecord,
    OperationObservabilityContext,
)

if TYPE_CHECKING:
    from .runtime import ObservabilityRuntime


class OperationLifecycle:
    def __init__(self, runtime: ObservabilityRuntime) -> None:
        self.runtime = runtime

    def operation(
        self,
        name: str,
        *,
        flavor: str | None = None,
        **attrs: Any,
    ):
        return _OperationManager(
            self.runtime, name, flavor=flavor, attrs=attrs
        )

    def entrypoint(
        self,
        name: str | None = None,
        *,
        flavor: str | None = None,
        **attrs: Any,
    ):
        def decorator(func):
            operation_name = name or getattr(func, "__name__", "operation")
            return self.runtime.operation(
                operation_name,
                flavor=flavor,
                **attrs,
            )(func)

        return decorator

    def start_operation(
        self,
        name: str,
        *,
        flavor: str | None = None,
        parent_context: Any = None,
        timings: dict[str, float] | None = None,
        emit_log: bool = True,
        record_metric: bool = True,
        **attrs: Any,
    ) -> dict[str, Any]:
        handle = {
            "operation": None,
            "operation_token": None,
            "timings_token": None,
            "start_token": None,
            "context_token": None,
        }
        if not self.runtime.enabled:
            return handle
        try:
            operation = OperationObservabilityContext(
                config=self.runtime.config,
                name=self.runtime._safe_str(name),
                flavor=flavor,
                attributes={
                    key: value
                    for key, value in attrs.items()
                    if value is not None
                },
                timings_ms={},
                emit_log=emit_log,
                record_metric=record_metric,
            )
            operation.context_token = _state._OPERATION_CONTEXT.set(operation)
            handle["operation"] = operation
            handle["operation_token"] = operation.context_token
            if timings is not None:
                handle["timings_token"] = _state._TIMINGS.set(timings)
            handle["start_token"] = _state._TURN_START.set(time.perf_counter())
            if parent_context is not None and self.runtime.tracer is not None:
                try:
                    from opentelemetry import context as otel_context

                    handle["context_token"] = otel_context.attach(
                        parent_context
                    )
                except BaseException as exc:
                    self.runtime.log_observability_failure(
                        "operation.context_attach",
                        exc,
                    )
            if self.runtime.tracer is not None:
                operation.span_handle = self.runtime._start_span(
                    self.runtime._span_name(operation.name),
                    operation.span_attributes(),
                )
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "operation.start", exc, name=name
            )
        return handle

    def end_operation(
        self,
        handle: dict[str, Any] | None,
        error: BaseException | None = None,
    ) -> None:
        if not handle:
            return
        operation = handle.get("operation")
        try:
            if operation is not None and error is not None:
                operation.error = ErrorRecord(
                    type=type(error).__name__,
                    message=self.runtime._safe_str(error),
                    handled=False,
                    stack=self.runtime._safe_traceback(error),
                )
                self.runtime.record_error_metric(
                    operation.metric_attributes(
                        error_type=type(error).__name__
                    )
                )
            if operation is not None:
                self.runtime.complete_operation(operation)
            if operation is not None:
                self.runtime._end_span(operation.span_handle, error)
        except BaseException as exc:
            self.runtime.log_observability_failure("operation.end", exc)
        finally:
            context_token = handle.get("context_token")
            if context_token is not None:
                try:
                    from opentelemetry import context as otel_context

                    otel_context.detach(context_token)
                except BaseException as exc:
                    self.runtime.log_observability_failure(
                        "operation.context_detach",
                        exc,
                    )
            for var, key in (
                (_state._TIMINGS, "timings_token"),
                (_state._TURN_START, "start_token"),
                (_state._OPERATION_CONTEXT, "operation_token"),
            ):
                token = handle.get(key)
                if token is not None:
                    try:
                        var.reset(token)
                    except BaseException as exc:
                        self.runtime.log_observability_failure(
                            "operation.context_reset",
                            exc,
                            token=key,
                        )

    def complete_operation(
        self,
        operation: OperationObservabilityContext,
    ) -> None:
        if operation.metric_recorded:
            return
        operation.metric_recorded = True
        if operation.record_metric:
            self.runtime.record_operation_metric(
                operation.duration_seconds(),
                operation.metric_attributes(),
            )
        if operation.emit_log:
            self.runtime.emit_operation_log(operation)

    @contextmanager
    def collect_timings(self, name: str = "operation", **attrs: Any):
        timings: dict[str, float] = {}
        handle = self.runtime.start_scope(timings, name=name, **attrs)
        error: BaseException | None = None
        try:
            yield timings
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.runtime.end_scope(handle, error)

    def start_scope(
        self,
        timings: dict[str, float],
        *,
        name: str = "operation",
        parent_context: Any = None,
        **attrs: Any,
    ) -> dict[str, Any]:
        if self.runtime.current_operation() is None:
            return {
                "operation_handle": self.runtime.start_operation(
                    name,
                    parent_context=parent_context,
                    timings=timings,
                    **attrs,
                )
            }
        handle = {
            "operation_handle": None,
            "timings_token": None,
            "start_token": None,
            "context_token": None,
            "span": None,
        }
        try:
            handle["timings_token"] = _state._TIMINGS.set(timings)
        except BaseException as exc:
            self.runtime.log_observability_failure("scope.timings_set", exc)
        try:
            handle["start_token"] = _state._TURN_START.set(time.perf_counter())
        except BaseException as exc:
            self.runtime.log_observability_failure("scope.start_set", exc)
        if parent_context is not None and self.runtime.tracer is not None:
            try:
                from opentelemetry import context as otel_context

                handle["context_token"] = otel_context.attach(parent_context)
            except BaseException as exc:
                self.runtime.log_observability_failure(
                    "scope.context_attach", exc
                )
        try:
            if self.runtime.tracer is not None:
                handle["span"] = self.runtime._start_span(name, attrs)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "scope.span_start", exc, span=name
            )
            handle["span"] = None
        return handle

    def annotate(
        self,
        handle: dict[str, Any] | None = None,
        **attrs: Any,
    ) -> None:
        try:
            if handle:
                span_handle = handle.get("span")
                if span_handle is not None:
                    _cm, span = span_handle
                    for key, value in attrs.items():
                        if value is not None:
                            span.set_attribute(key, value)
            context = self.runtime.current_context()
            if context is not None:
                for key, value in attrs.items():
                    context.set_attribute(key, value)
            operation = self.runtime.current_operation()
            if operation is not None:
                for key, value in attrs.items():
                    operation.set_attribute(key, value)
                self.runtime._set_current_span_attributes(
                    operation.span_attributes()
                )
        except BaseException as exc:
            self.runtime.log_observability_failure("scope.annotate", exc)

    def end_scope(
        self,
        handle: dict[str, Any] | None,
        error: BaseException | None = None,
    ) -> None:
        if not handle:
            return
        operation_handle = handle.get("operation_handle")
        if operation_handle is not None:
            self.runtime.end_operation(operation_handle, error)
            return
        try:
            self.runtime._end_span(handle.get("span"), error)
        except BaseException as exc:
            self.runtime.log_observability_failure("scope.span_end", exc)
        context_token = handle.get("context_token")
        if context_token is not None:
            try:
                from opentelemetry import context as otel_context

                otel_context.detach(context_token)
            except BaseException as exc:
                self.runtime.log_observability_failure(
                    "scope.context_detach", exc
                )
        for var, key in (
            (_state._TIMINGS, "timings_token"),
            (_state._TURN_START, "start_token"),
        ):
            token = handle.get(key)
            if token is not None:
                try:
                    var.reset(token)
                except BaseException as exc:
                    self.runtime.log_observability_failure(
                        "scope.context_reset",
                        exc,
                        token=key,
                    )

    def mark(self, key: str, ms: float) -> None:
        try:
            timings = _state._TIMINGS.get()
            if timings is not None:
                timings[key] = round(float(ms), 1)
        except BaseException as exc:
            self.runtime.log_observability_failure("scope.mark", exc, key=key)

    def mark_ttft(self, key: str = "ttft_ms") -> None:
        try:
            start = _state._TURN_START.get()
            if start is not None:
                self.runtime.mark(key, (time.perf_counter() - start) * 1000.0)
        except BaseException as exc:
            self.runtime.log_observability_failure("scope.mark_ttft", exc)

    def mark_ttft_attribute(self, key: str = "ttft_ms") -> None:
        try:
            start = _state._TURN_START.get()
            if start is None:
                return
            self.runtime.annotate(
                **{key: round((time.perf_counter() - start) * 1000.0, 1)}
            )
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "scope.mark_ttft_attribute", exc
            )

    def _start_implicit_operation(
        self,
        segment_name: str,
        attrs: dict[str, Any],
    ) -> dict[str, Any] | None:
        if (
            self.runtime.current_operation() is not None
            or self.runtime.current_context() is not None
        ):
            return None
        operation_name = attrs.get("operation") or segment_name
        flavor = attrs.get("flavor")
        operation_attrs = {
            key: value
            for key, value in attrs.items()
            if key not in {"operation", "flavor"} and value is not None
        }
        return self.runtime.start_operation(
            self.runtime._safe_str(operation_name),
            flavor=self.runtime._safe_str(flavor)
            if flavor is not None
            else None,
            **operation_attrs,
        )


class _OperationManager:
    def __init__(
        self,
        runtime: ObservabilityRuntime,
        name: str,
        *,
        flavor: str | None,
        attrs: dict[str, Any],
    ) -> None:
        self.runtime = runtime
        self.name = name
        self.flavor = flavor
        self.attrs = attrs
        self.handle: dict[str, Any] | None = None

    def __enter__(self):
        self.handle = self.runtime.start_operation(
            self.name,
            flavor=self.flavor,
            **self.attrs,
        )
        return self.runtime.current_operation()

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        self.runtime.end_operation(self.handle, exc)
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return self.__exit__(exc_type, exc, traceback)

    def __call__(self, func):
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                async with self.runtime.operation(
                    self.name,
                    flavor=self.flavor,
                    **self.attrs,
                ):
                    return await func(*args, **kwargs)

            return async_wrapper

        @wraps(func)
        def wrapper(*args, **kwargs):
            with self.runtime.operation(
                self.name,
                flavor=self.flavor,
                **self.attrs,
            ):
                return func(*args, **kwargs)

        return wrapper
