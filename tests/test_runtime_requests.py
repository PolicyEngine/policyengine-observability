from __future__ import annotations

from contextvars import ContextVar
from types import SimpleNamespace

from runtime_helpers import (
    NamedRecordingSpan,
    RecordingInstrument,
    SegmentName,
    runtime,
)

from policyengine_observability import (
    RequestObservabilityContext,
)
from policyengine_observability import _state as state_module


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


def test_request_log_accumulates_repeated_segment_timings() -> None:
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
        inbound={},
    )

    observed.begin_request(context)
    with observed.segment(SegmentName.LOAD):
        pass
    with observed.segment(SegmentName.LOAD):
        pass
    observed.finish_request(200)
    observed.teardown_request(None)

    assert context.timings_ms["load"] >= 0
    assert context.timing_counts["load"] == 2
    payload = context.as_log_record(trace_id=None, span_id=None)
    assert payload["timing_counts"]["load"] == 2
    assert [node["name"] for node in payload["segment_tree"]] == [
        "load",
        "load",
    ]


def test_internal_dispatch_segments_merge_into_parent_operation() -> None:
    observed = runtime()
    handle = observed.start_operation(
        "modal_worker_dispatch",
        flavor="modal_worker",
    )
    parent_operation = handle["operation"]
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="POST",
        route="/calculate",
        path="/calculate",
        endpoint="calculate",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
        internal_dispatch=True,
    )

    try:
        observed.begin_request(context)
        with observed.segment(SegmentName.LOAD):
            pass
        observed.finish_request(200)
        observed.teardown_request(None)

        assert context.timings_ms is parent_operation.timings_ms
        assert context.timing_counts is parent_operation.timing_counts
        assert context.segment_tree is parent_operation.segment_tree
        assert "load" in parent_operation.timings_ms
        assert parent_operation.timing_counts["load"] == 1
        assert parent_operation.segment_tree[0].name == "load"
        assert observed.current_operation() is parent_operation
    finally:
        observed.end_operation(handle)

    assert observed.current_context() is None
    assert observed.current_operation() is None


def test_non_internal_request_timings_do_not_leak_to_parent_operation() -> (
    None
):
    observed = runtime()
    handle = observed.start_operation("job", flavor="worker")
    parent_operation = handle["operation"]
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="POST",
        route="/calculate",
        path="/calculate",
        endpoint="calculate",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    try:
        observed.begin_request(context)
        with observed.segment(SegmentName.LOAD):
            pass
        observed.finish_request(200)
        observed.teardown_request(None)

        assert context.timings_ms is not parent_operation.timings_ms
        assert context.segment_tree is not parent_operation.segment_tree
        assert "load" not in parent_operation.timings_ms
        assert parent_operation.segment_tree == []
        assert context.segment_tree[0].name == "load"
        assert observed.current_operation() is parent_operation
    finally:
        observed.end_operation(handle)

    assert observed.current_context() is None
    assert observed.current_operation() is None


def test_set_attribute_updates_explicit_operation_inside_request() -> None:
    observed = runtime()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/chat",
        path="/chat",
        endpoint="chat",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    observed.begin_request(context)
    handle = observed.start_operation("chat.turn", flavor="chat")
    operation = handle["operation"]
    try:
        observed.set_attribute("model", "claude")
    finally:
        observed.end_operation(handle)
        observed.teardown_request(None)

    assert context.attributes["model"] == "claude"
    assert context.operation_context.attributes["model"] == "claude"
    assert operation.attributes["model"] == "claude"
    assert observed.current_context() is None
    assert observed.current_operation() is None


def test_mark_ttft_attribute_updates_current_operation() -> None:
    observed = runtime()
    handle = observed.start_operation("chat.turn", flavor="chat")
    operation = handle["operation"]

    try:
        observed.mark_ttft_attribute()
    finally:
        observed.end_operation(handle)

    assert operation.attributes["ttft_ms"] >= 0


def test_request_methods_noop_without_current_context() -> None:
    observed = runtime()

    assert observed.prepare_response(200) == {}
    observed.complete_request(200)
    observed.update_request_route(route="/missing")
    observed.teardown_request(None)


def test_request_begin_operation_begin_and_lifecycle_failures_are_logged(
    monkeypatch,
) -> None:
    class BrokenVar:
        def set(self, _value):
            raise RuntimeError("set failed")

    observed = runtime()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/broken",
        path="/broken",
        endpoint="broken",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )

    monkeypatch.setattr(state_module, "_REQUEST_CONTEXT", BrokenVar())
    observed.begin_request(context)
    monkeypatch.setattr(
        state_module,
        "_REQUEST_CONTEXT",
        ContextVar("request", default=None),
    )
    monkeypatch.setattr(state_module, "_OPERATION_CONTEXT", BrokenVar())
    observed._begin_request_operation(context)

    assert failures == ["request.begin", "request.operation_begin"]


def test_request_prepare_complete_update_and_teardown_failures_are_logged() -> (
    None
):
    observed = runtime()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/broken",
        path="/broken",
        endpoint="broken",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )
    observed.begin_request(context)
    context.span_attributes = lambda **_extra: (_ for _ in ()).throw(
        RuntimeError("span attrs failed")
    )
    observed.prepare_response(200)
    observed.complete_request(200)
    observed.update_request_route(route="/other")
    observed.emit_request_log = lambda _context: (_ for _ in ()).throw(
        RuntimeError("emit failed")
    )
    observed.teardown_request(None)

    assert "request.prepare_response" in failures
    assert "request.complete" in failures
    assert "request.update_route" in failures
    assert "request.teardown" in failures


def test_set_attribute_failure_path_is_logged() -> None:
    observed = runtime()
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    observed.current_context = lambda: SimpleNamespace(
        set_attribute=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("attribute failed")
        )
    )

    observed.set_attribute("tool", "loader")

    assert failures == ["request.set_attribute"]


def test_request_route_update_relabels_active_request_and_span() -> None:
    observed = runtime()
    observed.active_requests = RecordingInstrument()
    span = NamedRecordingSpan()
    context = RequestObservabilityContext(
        config=observed.config,
        request_id="request-1",
        method="GET",
        route="/initial",
        path="/items/1",
        endpoint="initial",
        query_keys=[],
        content_length_bytes=None,
        inbound={},
    )
    context.server_span = span

    observed.begin_request(context)
    observed.update_request_route(route="/items/<id>", endpoint="item")
    observed.teardown_request(None)

    assert context.route == "/items/<id>"
    assert context.endpoint == "item"
    assert span.names == ["/items/<id>"]
    assert observed.active_requests.calls[1][1] == -1
    assert observed.active_requests.calls[2][1] == 1
