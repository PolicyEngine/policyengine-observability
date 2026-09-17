"""Request lifecycle, response metadata, and cleanup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import _state
from ._state import REQUEST_ID_HEADER, TRACEPARENT_HEADER
from .context import (
    OperationObservabilityContext,
    RequestObservabilityContext,
)

if TYPE_CHECKING:
    from .runtime import ObservabilityRuntime


class RequestLifecycle:
    def __init__(self, runtime: ObservabilityRuntime) -> None:
        self.runtime = runtime

    def begin_request(
        self,
        context: RequestObservabilityContext,
        *,
        carrier: Any = None,
    ) -> None:
        if not self.runtime.enabled:
            return
        try:
            context.context_token = _state._REQUEST_CONTEXT.set(context)
            context.set_attribute("endpoint", context.endpoint)
            self.runtime._begin_request_operation(context)
            self.runtime._start_request_span(context, carrier=carrier)
            self.runtime.record_active_request(1, context.metric_attributes())
        except BaseException as exc:
            self.runtime.log_observability_failure("request.begin", exc)

    def _begin_request_operation(
        self,
        context: RequestObservabilityContext,
    ) -> None:
        try:
            parent_operation = _state._OPERATION_CONTEXT.get()
            timings = context.timings_ms
            timing_counts = context.timing_counts
            segment_tree = context.segment_tree
            segment_sequence = context.segment_sequence
            if context.internal_dispatch and parent_operation is not None:
                timings = parent_operation.timings_ms
                timing_counts = parent_operation.timing_counts
                segment_tree = parent_operation.segment_tree
                segment_sequence = parent_operation.segment_sequence
                context.timings_ms = timings
                context.timing_counts = timing_counts
                context.segment_tree = segment_tree
                context.segment_sequence = segment_sequence
            operation = OperationObservabilityContext(
                config=context.config,
                name=context.route,
                flavor="http",
                attributes={
                    "route": context.route,
                    "method": context.method,
                    "endpoint": context.endpoint,
                    "path": context.path,
                },
                timings_ms=timings,
                timing_counts=timing_counts,
                segment_tree=segment_tree,
                segment_sequence=segment_sequence,
                emit_log=False,
                record_metric=False,
            )
            operation.context_token = _state._OPERATION_CONTEXT.set(operation)
            context.operation_context = operation
            context.operation_token = operation.context_token
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "request.operation_begin",
                exc,
                request_id=getattr(context, "request_id", None),
            )

    def finish_request(self, status_code: int) -> dict[str, str]:
        headers = self.runtime.prepare_response(status_code)
        self.runtime.complete_request(status_code)
        return headers

    def prepare_response(self, status_code: int) -> dict[str, str]:
        if not self.runtime.enabled:
            return {}
        headers: dict[str, str] = {}
        try:
            context = self.runtime.current_context()
            if context is None:
                return headers
            context.status_code = status_code
            self.runtime._set_current_span_attributes(
                context.span_attributes()
            )
            if context.operation_context is not None:
                context.operation_context.set_attribute(
                    "status_code",
                    str(status_code),
                )
            headers[REQUEST_ID_HEADER] = context.request_id
            traceparent = self.runtime.traceparent_header()
            if traceparent:
                headers[TRACEPARENT_HEADER] = traceparent
            if status_code == 429:
                context.set_attribute("rate_limited", True)
            return headers
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "request.prepare_response", exc
            )
        return headers

    def complete_request(self, status_code: int | None = None) -> None:
        if not self.runtime.enabled:
            return
        try:
            context = self.runtime.current_context()
            if context is None:
                return
            if status_code is not None:
                context.status_code = status_code
                self.runtime._set_current_span_attributes(
                    context.span_attributes()
                )
            if context.request_metric_recorded:
                return
            context.request_metric_recorded = True
            if context.status_code == 429:
                self.runtime.record_rate_limited_metric(
                    context.metric_attributes()
                )
            self.runtime.record_request_metric(
                context.duration_seconds(),
                context.metric_attributes(),
            )
            self.runtime._close_active_request(context)
        except BaseException as exc:
            self.runtime.log_observability_failure("request.complete", exc)

    def update_request_route(
        self,
        *,
        route: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        if not self.runtime.enabled:
            return
        try:
            context = self.runtime.current_context()
            if context is None:
                return
            route_changed = bool(route and route != context.route)
            old_active_attributes = (
                context.metric_attributes()
                if route_changed and not context.active_closed
                else None
            )
            if route:
                context.route = route
                if context.operation_context is not None:
                    context.operation_context.name = route
                    context.operation_context.set_attribute("route", route)
            if endpoint:
                context.endpoint = endpoint
                context.set_attribute("endpoint", endpoint)
                if context.operation_context is not None:
                    context.operation_context.set_attribute(
                        "endpoint",
                        endpoint,
                    )
            self.runtime._set_current_span_attributes(
                context.span_attributes()
            )
            span = context.server_span
            update_name = getattr(span, "update_name", None)
            if route and update_name is not None:
                update_name(route)
            if old_active_attributes is not None:
                self.runtime.record_active_request(-1, old_active_attributes)
                self.runtime.record_active_request(
                    1, context.metric_attributes()
                )
        except BaseException as exc:
            self.runtime.log_observability_failure("request.update_route", exc)

    def teardown_request(self, exc: BaseException | None = None) -> None:
        if not self.runtime.enabled:
            return
        context = self.runtime.current_context()
        if context is None:
            return
        try:
            if exc is not None:
                self.runtime.record_error(
                    exc,
                    handled=False,
                    status_code=context.status_code or 500,
                )
            self.runtime._close_active_request(context)
            self.runtime.emit_request_log(context)
        except BaseException as observability_exc:
            self.runtime.log_observability_failure(
                "request.teardown",
                observability_exc,
            )
        finally:
            self.runtime._close_request_span(context, exc)
            self.runtime._reset_request_operation_context(context)
            self.runtime._reset_request_context(context)

    def set_attribute(self, key: str, value: Any) -> None:
        if not self.runtime.enabled:
            return
        try:
            context = self.runtime.current_context()
            if context is not None:
                context.set_attribute(key, value)
                if context.operation_context is not None:
                    context.operation_context.set_attribute(key, value)
                self.runtime._set_current_span_attributes(
                    context.span_attributes(**{f"policyengine.{key}": value})
                )
            operation = self.runtime.current_operation()
            if operation is not None and operation is not getattr(
                context, "operation_context", None
            ):
                operation.set_attribute(key, value)
                self.runtime._set_current_span_attributes(
                    operation.span_attributes(**{f"policyengine.{key}": value})
                )
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "request.set_attribute",
                exc,
                attribute=key,
            )

    def _close_active_request(
        self,
        context: RequestObservabilityContext,
    ) -> None:
        try:
            if context.active_closed:
                return
            context.active_closed = True
            self.runtime.record_active_request(-1, context.metric_attributes())
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "request.close_active",
                exc,
                request_id=getattr(context, "request_id", None),
            )

    def _reset_request_operation_context(
        self,
        context: RequestObservabilityContext,
    ) -> None:
        token = context.operation_token
        if token is None:
            return
        try:
            _state._OPERATION_CONTEXT.reset(token)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "request.operation_context_reset",
                exc,
                request_id=getattr(context, "request_id", None),
            )

    def _reset_request_context(
        self,
        context: RequestObservabilityContext,
    ) -> None:
        token = context.context_token
        if token is None:
            return
        try:
            _state._REQUEST_CONTEXT.reset(token)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "request.context_reset",
                exc,
                request_id=getattr(context, "request_id", None),
            )
