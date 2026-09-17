from __future__ import annotations

from types import SimpleNamespace

from runtime_helpers import (
    FailingInstrument,
    FailingLogDestination,
    RecordingInstrument,
    RecordingLogDestination,
    SegmentName,
    runtime,
)

from policyengine_observability import (
    ObservabilityConfig,
    ObservabilityRuntime,
    RequestObservabilityContext,
    coerce_segment_name,
)
from policyengine_observability.destinations import (
    google_cloud_logging as google_cloud_logging_module,
)


def test_trace_helpers_log_failures_without_throwing() -> None:
    observed = runtime()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    observed.propagate = SimpleNamespace(
        inject=lambda _carrier: (_ for _ in ()).throw(RuntimeError("inject")),
        extract=lambda _carrier: (_ for _ in ()).throw(
            RuntimeError("extract")
        ),
    )
    observed.trace = SimpleNamespace(
        get_current_span=lambda: (_ for _ in ()).throw(RuntimeError("span"))
    )

    assert observed.traceparent_header() is None
    assert observed._extract_context({"traceparent": "parent"}) is None
    assert observed._current_span() is None
    assert failures == [
        "request.traceparent_header",
        "otel.extract_context",
        "otel.current_span",
    ]


def test_record_event_covers_operation_context_and_no_context_metrics() -> (
    None
):
    observed = runtime()
    observed.failover_events = RecordingInstrument()

    with observed.operation("worker", flavor="queue"):
        observed.record_event("modal_retry", attempt=1, ignored=None)

    observed.record_event("fallback_without_context")

    assert len(observed.failover_events.calls) == 2
    assert observed.failover_events.calls[0][2]["operation"] == "worker"
    assert observed.failover_events.calls[1][2]["event"] == (
        "fallback_without_context"
    )


def test_record_event_request_context_and_emit_log_skip_paths() -> None:
    observed = runtime()
    observed.failover_events = RecordingInstrument()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/event",
        path="/event",
        endpoint="event",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
        internal_dispatch=True,
    )
    observed.begin_request(context)
    observed.record_event("fallback_request", detail="request")
    observed.emit_request_log(context)
    observed.emit_request_log(context)
    observed.teardown_request(None)

    operation = observed.start_operation("job")["operation"]
    observed.emit_operation_log(operation)
    observed.emit_operation_log(operation)
    observed.end_operation({"operation": operation})

    assert observed.failover_events.calls[0][2]["route"] == "/event"
    assert context.emitted is True
    assert operation.emitted is True


def test_operation_log_emits_to_configured_destinations_once() -> None:
    observed = runtime()
    destination = RecordingLogDestination()
    observed.log_destination_manager.destinations = [destination]
    observed.log_destination_manager.configured = True

    handle = observed.start_operation("job")
    operation = handle["operation"]
    observed.end_operation(handle)
    observed.emit_operation_log(operation)

    assert len(destination.calls) == 1
    payload, log_type, severity = destination.calls[0]
    assert payload["operation"] == "job"
    assert payload["severity"] == "INFO"
    assert log_type == "operation"
    assert severity == "INFO"


def test_request_log_emits_to_configured_destination_once() -> None:
    observed = runtime()
    destination = RecordingLogDestination()
    observed.log_destination_manager.destinations = [destination]
    observed.log_destination_manager.configured = True
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/calculate",
        path="/calculate",
        endpoint="calculate",
        query_keys=[],
        content_length_bytes=None,
        inbound={"client_ip": "203.0.113.1"},
        status_code=200,
    )

    observed.emit_request_log(context)
    observed.emit_request_log(context)

    assert len(destination.calls) == 1
    payload, log_type, severity = destination.calls[0]
    assert payload["request_id"] == "request-1"
    assert payload["client_ip"] == "203.0.113.1"
    assert payload["severity"] == "INFO"
    assert log_type == "request"
    assert severity == "INFO"


def test_request_log_reserved_fields_override_inbound_and_attributes() -> None:
    observed = runtime()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/calculate",
        path="/calculate",
        endpoint="calculate",
        query_keys=[],
        content_length_bytes=None,
        inbound={
            "request_id": "inbound-request",
            "status_code": "inbound-status",
            "duration_ms": "inbound-duration",
            "segment_tree": "inbound-tree",
        },
        attributes={
            "request_id": "attribute-request",
            "status_code": "attribute-status",
            "duration_ms": "attribute-duration",
            "timings_ms": "attribute-timings",
            "timing_counts": "attribute-counts",
            "segment_tree": "attribute-tree",
            "error": "attribute-error",
        },
        status_code=204,
    )

    payload = context.as_log_record(trace_id=None, span_id=None)
    assert payload["request_id"] == "request-1"
    assert payload["status_code"] == 204
    assert isinstance(payload["duration_ms"], float)
    assert isinstance(payload["timings_ms"], dict)
    assert isinstance(payload["timing_counts"], dict)
    assert payload["segment_tree"] == []
    assert payload["error"] is None


def test_event_log_emits_to_configured_destination() -> None:
    observed = runtime()
    destination = RecordingLogDestination()
    observed.log_destination_manager.destinations = [destination]
    observed.log_destination_manager.configured = True

    observed.record_event("custom_event", detail="value")

    assert len(destination.calls) == 1
    payload, log_type, severity = destination.calls[0]
    assert payload["event"] == "custom_event"
    assert payload["detail"] == "value"
    assert payload["service_name"] == "svc"
    assert payload["severity"] == "INFO"
    assert log_type == "event"
    assert severity == "INFO"


def test_log_severity_maps_status_codes_and_errors() -> None:
    observed = runtime()

    assert observed._severity_for_log_record({"status_code": 200}) == "INFO"
    assert observed._severity_for_log_record({"status_code": 404}) == (
        "WARNING"
    )
    assert observed._severity_for_log_record({"status_code": "500"}) == (
        "ERROR"
    )
    assert (
        observed._severity_for_log_record({"error": {"handled": False}})
        == "ERROR"
    )
    assert observed._severity_for_log_record({"error": {"handled": True}}) == (
        "WARNING"
    )


def test_destination_failure_logs_internal_error_without_throwing() -> None:
    observed = runtime()
    recording = RecordingLogDestination()
    observed.log_destination_manager.destinations = [
        FailingLogDestination(),
        recording,
    ]
    observed.log_destination_manager.configured = True

    with observed.operation("job"):
        pass

    assert any(
        payload["event"] == "observability_internal_error"
        and payload["operation"] == "logging.destination_emit"
        for payload, _log_type, _severity in recording.calls
    )
    assert any(
        payload.get("operation") == "job"
        for payload, _log_type, _severity in recording.calls
    )


def test_all_destination_failures_fall_back_to_stderr(capsys) -> None:
    observed = runtime()
    observed.log_destination_manager.destinations = [FailingLogDestination()]
    observed.log_destination_manager.configured = True

    with observed.operation("job"):
        pass

    stderr = capsys.readouterr().err
    assert "observability_internal_error" in stderr
    assert "logging.destination_emit" in stderr


def test_unknown_destination_falls_back_to_stdout() -> None:
    observed = ObservabilityRuntime(
        ObservabilityConfig(
            service_name="svc",
            otel_enabled=False,
            log_destinations=("missing",),
        )
    )

    observed.configure()

    assert [
        destination.name
        for destination in observed.log_destination_manager.destinations
    ] == ["stdout"]


def test_google_destination_init_failure_falls_back_to_stdout(
    monkeypatch,
) -> None:
    def fail_google_destination(**_kwargs):
        raise ImportError("google-cloud-logging missing")

    monkeypatch.setattr(
        google_cloud_logging_module,
        "GoogleCloudLoggingDestination",
        fail_google_destination,
    )
    observed = ObservabilityRuntime(
        ObservabilityConfig(
            service_name="svc",
            otel_enabled=False,
            log_destinations=("google_cloud_logging",),
        )
    )

    observed.configure()

    assert [
        destination.name
        for destination in observed.log_destination_manager.destinations
    ] == ["stdout"]


def test_disabled_configure_does_not_initialize_log_destinations(
    monkeypatch,
) -> None:
    def fail_google_destination(**_kwargs):
        raise AssertionError("google destination should not initialize")

    monkeypatch.setattr(
        google_cloud_logging_module,
        "GoogleCloudLoggingDestination",
        fail_google_destination,
    )
    observed = ObservabilityRuntime(
        ObservabilityConfig(
            service_name="svc",
            enabled=False,
            log_destinations=("google_cloud_logging",),
        )
    )

    observed.configure()

    assert observed.log_destination_manager.destinations == []
    assert observed.log_destination_manager.configured is False


def test_record_segment_metric_covers_calculation_and_backend() -> None:
    observed = runtime()
    observed.segment_duration = RecordingInstrument()
    observed.calculate_duration = RecordingInstrument()
    observed.backend_duration = RecordingInstrument()

    observed.record_segment_metric(
        "calculation",
        0.1,
        {"backend": "modal"},
        backend_segment=True,
    )

    assert observed.segment_duration.calls
    assert observed.calculate_duration.calls
    assert observed.backend_duration.calls


def test_metric_recording_failures_are_logged() -> None:
    observed = runtime()
    observed.operation_duration = FailingInstrument()
    observed.http_duration = FailingInstrument()
    observed.segment_duration = FailingInstrument()
    observed.errors = FailingInstrument()
    observed.rate_limited = FailingInstrument()
    observed.failover_events = FailingInstrument()
    observed.active_requests = FailingInstrument()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )

    observed.record_operation_metric(0.1, {})
    observed.record_request_metric(0.1, {})
    observed.record_segment_metric("load", 0.1, {})
    observed.record_error_metric({})
    observed.record_rate_limited_metric({})
    observed.record_failover_event_metric({})
    observed.record_active_request(1, {})

    assert failures == [
        "metrics.record_operation",
        "metrics.record_request",
        "metrics.record_segment",
        "metrics.record_error",
        "metrics.record_rate_limited",
        "metrics.record_failover_event",
        "metrics.add_active_request",
    ]


def test_private_safety_helpers_cover_fallback_paths(
    monkeypatch,
    capsys,
) -> None:
    observed = runtime()

    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("cannot stringify")

    assert observed._safe_str(Unprintable()) == "<unprintable Unprintable>"
    assert observed._safe_traceback(Unprintable()) == ""

    original_dumps = __import__("json").dumps

    def failing_dumps(payload, *args, **kwargs):
        if payload.get("event") == "bad":
            raise RuntimeError("json failed")
        return original_dumps(payload, *args, **kwargs)

    monkeypatch.setattr("json.dumps", failing_dumps)
    assert "observability_internal_error" in observed._json({"event": "bad"})

    monkeypatch.setattr(
        "policyengine_observability.runtime.INTERNAL_LOGGER.error",
        lambda _message: (_ for _ in ()).throw(RuntimeError("logger failed")),
    )
    observed.log_observability_failure("test", RuntimeError("failed"))
    assert "observability_internal_error" in capsys.readouterr().err


def test_coerce_segment_name_validates_registry() -> None:
    assert coerce_segment_name(SegmentName.LOAD, registry=SegmentName) == (
        "load",
        True,
    )
    assert coerce_segment_name("other", registry=SegmentName) == (
        "other",
        False,
    )
    assert coerce_segment_name("load", registry=["load"]) == ("load", True)
    assert coerce_segment_name(SegmentName.LOAD, registry=None) == (
        "load",
        True,
    )
