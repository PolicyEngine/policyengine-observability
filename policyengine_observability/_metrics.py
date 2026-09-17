"""Metric instrument creation and recording."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import ObservabilityRuntime


class _NoOpInstrument:
    def add(self, *_args, **_kwargs) -> None:
        return None

    def record(self, *_args, **_kwargs) -> None:
        return None


class MetricRecorder:
    def __init__(self, runtime: ObservabilityRuntime) -> None:
        self.runtime = runtime

    def record_operation_metric(
        self,
        duration_seconds: float,
        attributes: dict[str, str],
    ) -> None:
        try:
            self.runtime.operation_duration.record(
                duration_seconds, attributes
            )
            self.runtime.operations.add(1, attributes)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "metrics.record_operation", exc
            )

    def record_request_metric(
        self,
        duration_seconds: float,
        attributes: dict[str, str],
    ) -> None:
        try:
            self.runtime.http_duration.record(duration_seconds, attributes)
            self.runtime.requests.add(1, attributes)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "metrics.record_request", exc
            )

    def record_segment_metric(
        self,
        segment: str,
        duration_seconds: float,
        attributes: dict[str, str],
        *,
        backend_segment: bool = False,
    ) -> None:
        try:
            segment_attributes = {**attributes, "segment": segment}
            self.runtime.segment_duration.record(
                duration_seconds, segment_attributes
            )
            if segment == "calculation":
                self.runtime.calculate_duration.record(
                    duration_seconds, attributes
                )
            if backend_segment:
                self.runtime.backend_duration.record(
                    duration_seconds,
                    segment_attributes,
                )
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "metrics.record_segment",
                exc,
                segment=segment,
            )

    def record_error_metric(self, attributes: dict[str, str]) -> None:
        try:
            self.runtime.errors.add(1, attributes)
        except BaseException as exc:
            self.runtime.log_observability_failure("metrics.record_error", exc)

    def record_rate_limited_metric(self, attributes: dict[str, str]) -> None:
        try:
            self.runtime.rate_limited.add(1, attributes)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "metrics.record_rate_limited", exc
            )

    def record_failover_event_metric(self, attributes: dict[str, str]) -> None:
        try:
            self.runtime.failover_events.add(1, attributes)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "metrics.record_failover_event",
                exc,
            )

    def record_active_request(
        self,
        delta: int,
        attributes: dict[str, str],
    ) -> None:
        try:
            self.runtime.active_requests.add(delta, attributes)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "metrics.add_active_request", exc
            )

    def _configure_instruments(self) -> None:
        self.runtime.operation_duration = self.runtime._instrument(
            getattr(self.runtime.meter, "create_histogram", None),
            "policyengine.operation.duration",
            unit="s",
            description="PolicyEngine operation duration.",
        )
        self.runtime.http_duration = self.runtime._instrument(
            getattr(self.runtime.meter, "create_histogram", None),
            "http.server.request.duration",
            unit="s",
            description="HTTP server request duration.",
        )
        self.runtime.segment_duration = self.runtime._instrument(
            getattr(self.runtime.meter, "create_histogram", None),
            "policyengine.segment.duration",
            unit="s",
            description="PolicyEngine operation segment duration.",
        )
        self.runtime.calculate_duration = self.runtime._instrument(
            getattr(self.runtime.meter, "create_histogram", None),
            "policyengine.calculate.duration",
            unit="s",
            description="PolicyEngine calculate operation duration.",
        )
        self.runtime.backend_duration = self.runtime._instrument(
            getattr(self.runtime.meter, "create_histogram", None),
            "policyengine.backend.duration",
            unit="s",
            description="PolicyEngine backend call duration.",
        )
        self.runtime.operations = self.runtime._instrument(
            getattr(self.runtime.meter, "create_counter", None),
            "policyengine.operations",
            description="PolicyEngine operation count.",
        )
        self.runtime.requests = self.runtime._instrument(
            getattr(self.runtime.meter, "create_counter", None),
            "policyengine.requests",
            description="PolicyEngine request count.",
        )
        self.runtime.errors = self.runtime._instrument(
            getattr(self.runtime.meter, "create_counter", None),
            "policyengine.errors",
            description="PolicyEngine error count.",
        )
        self.runtime.rate_limited = self.runtime._instrument(
            getattr(self.runtime.meter, "create_counter", None),
            "policyengine.rate_limited_requests",
            description="PolicyEngine rate-limited request count.",
        )
        self.runtime.failover_events = self.runtime._instrument(
            getattr(self.runtime.meter, "create_counter", None),
            "policyengine.failover.events",
            description="PolicyEngine failover event count.",
        )
        self.runtime.active_requests = self.runtime._instrument(
            getattr(self.runtime.meter, "create_up_down_counter", None),
            "http.server.active_requests",
            description="Active HTTP server requests.",
        )

    def _instrument(self, factory, *args, **kwargs):
        if factory is None:
            return _NoOpInstrument()
        try:
            return factory(*args, **kwargs)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "metrics.create_instrument",
                exc,
                instrument=args[0] if args else None,
            )
            return _NoOpInstrument()
