from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from conftest import make_runtime, records
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from flask import Flask
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from policyengine_observability import (
    REQUEST_ID_HEADER,
    OTelConfig,
    instrument_fastapi,
    instrument_flask,
    instrument_httpx,
)


def test_flask_lifecycle_and_idempotence() -> None:
    runtime, output = make_runtime()
    unused, _ = make_runtime()
    app = Flask(__name__)

    @app.get("/items/<item_id>")
    def item(item_id: str):
        return {"item": item_id}

    assert instrument_flask(app, runtime) is runtime
    assert instrument_flask(app, unused) is runtime
    response = app.test_client().get(
        "/items/abc", headers={REQUEST_ID_HEADER: "flask-request"}
    )
    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == "flask-request"
    emitted = records(output)
    assert len(emitted) == 1
    assert emitted[0]["http.route"] == "/items/<item_id>"
    runtime.shutdown()
    unused.shutdown()


def test_flask_exception_emits_one_error_completion() -> None:
    runtime, output = make_runtime(otel=OTelConfig(enabled=True))
    exporter = InMemorySpanExporter()
    runtime._otel._tracer_provider.add_span_processor(
        SimpleSpanProcessor(exporter)
    )
    app = Flask(__name__)

    @app.get("/fail")
    def fail():
        raise ValueError("route failed")

    instrument_flask(app, runtime)
    response = app.test_client().get("/fail")
    assert response.status_code == 500
    emitted = records(output)
    assert len(emitted) == 1
    assert emitted[0]["outcome"] == "error"
    assert emitted[0]["error.type"] == "ValueError"
    assert emitted[0]["error.message"] == "route failed"
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.events[0].name == "exception"
    assert span.events[0].attributes["exception.message"] == "route failed"
    runtime.shutdown()


def test_flask_error_handler_exception_is_kept_on_request_completion(
    monkeypatch,
) -> None:
    runtime, output = make_runtime(otel=OTelConfig(enabled=True))
    exporter = InMemorySpanExporter()
    runtime._otel._tracer_provider.add_span_processor(
        SimpleSpanProcessor(exporter)
    )
    error_metric_points = _capture_error_metric_points(monkeypatch, runtime)
    app = Flask(__name__)

    @app.get("/fail")
    def fail_calculation():
        raise ValueError("route failed")

    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        runtime.record_exception(error, handled=False, status_code=500)
        return {"error": "internal"}, 500

    instrument_flask(app, runtime)
    response = app.test_client().get("/fail")

    assert response.status_code == 500
    completions = _request_completions(records(output))
    assert len(completions) == 1
    completion = completions[0]
    assert completion["http.response.status_code"] == 500
    assert completion["outcome"] == "error"
    assert completion["severity"] == "ERROR"
    assert completion["error.type"] == "ValueError"
    assert completion["error.message"] == "route failed"
    assert completion["error.stack"].startswith("Traceback")
    assert "fail_calculation" in completion["error.stack"]
    assert "ValueError: route failed" in completion["error.stack"]
    assert [point["operation.kind"] for point in error_metric_points] == [
        "unhandled",
        "request",
    ]
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert [event.name for event in span.events] == ["exception"]
    assert span.events[0].attributes["exception.type"] == "ValueError"
    assert span.events[0].attributes["exception.message"] == "route failed"
    runtime.shutdown()


@pytest.mark.parametrize(
    ("recorded_status_code", "response_status_code", "expected_outcome"),
    ((None, 200, "success"), (404, 404, "client_error")),
)
def test_flask_handled_exception_does_not_mark_request_failed(
    monkeypatch,
    recorded_status_code,
    response_status_code,
    expected_outcome,
) -> None:
    runtime, output = make_runtime(otel=OTelConfig(enabled=True))
    exporter = InMemorySpanExporter()
    runtime._otel._tracer_provider.add_span_processor(
        SimpleSpanProcessor(exporter)
    )
    error_metric_points = _capture_error_metric_points(monkeypatch, runtime)
    app = Flask(__name__)

    @app.get("/items/<item_id>")
    def load_item(item_id: str):
        try:
            raise LookupError(f"item {item_id} is not cached")
        except LookupError as error:
            runtime.record_exception(
                error,
                handled=True,
                status_code=recorded_status_code,
            )
        return {"item": item_id}, response_status_code

    instrument_flask(app, runtime)
    response = app.test_client().get("/items/abc")

    assert response.status_code == response_status_code
    emitted = records(output)
    assert [item["event.name"] for item in emitted] == [
        "exception.recorded",
        "request.completed",
    ]
    completion = emitted[-1]
    assert completion["http.response.status_code"] == response_status_code
    assert completion["outcome"] == expected_outcome
    assert completion["severity"] == "INFO"
    assert _error_field_names(completion) == []
    assert [point["operation.kind"] for point in error_metric_points] == [
        "handled"
    ]
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.UNSET
    assert span.events == ()
    runtime.shutdown()


def test_flask_observes_early_before_request_response() -> None:
    runtime, output = make_runtime()
    app = Flask(__name__)

    @app.before_request
    def reject_request():
        return {"error": "unauthorized"}, 401

    instrument_flask(app, runtime)
    response = app.test_client().get("/protected")

    assert response.status_code == 401
    assert REQUEST_ID_HEADER in response.headers
    emitted = records(output)
    assert len(emitted) == 1
    assert emitted[0]["http.response.status_code"] == 401
    assert emitted[0]["outcome"] == "client_error"
    runtime.shutdown()


def test_flask_late_installation_is_nonfatal_and_leaves_no_state() -> None:
    runtime, output = make_runtime()
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    client = app.test_client()
    assert client.get("/health").status_code == 200
    callbacks_before = _flask_callback_snapshot(app)

    assert instrument_flask(app, runtime) is runtime

    assert _flask_callback_snapshot(app) == callbacks_before
    assert "policyengine_observability" not in app.extensions
    assert runtime.diagnostics.count("failure.flask.callback_install") == 1
    assert client.get("/health").status_code == 200
    assert records(output) == []
    runtime.shutdown()


@pytest.mark.parametrize(
    "registration_name",
    ("before_request", "after_request", "teardown_request"),
)
def test_flask_installation_failure_rolls_back_and_can_retry(
    monkeypatch, registration_name
) -> None:
    runtime, output = make_runtime()
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    callbacks_before = _flask_callback_snapshot(app)
    original = getattr(app, registration_name)

    def register_then_fail(callback):
        original(callback)
        raise RuntimeError("registration failed")

    monkeypatch.setattr(app, registration_name, register_then_fail)

    assert instrument_flask(app, runtime) is runtime
    assert _flask_callback_snapshot(app) == callbacks_before
    assert "policyengine_observability" not in app.extensions
    assert runtime.diagnostics.count("failure.flask.callback_install") == 1

    monkeypatch.setattr(app, registration_name, original)
    assert instrument_flask(app, runtime) is runtime
    response = app.test_client().get("/health")

    assert response.status_code == 200
    assert REQUEST_ID_HEADER in response.headers
    assert len(records(output)) == 1
    runtime.shutdown()


def test_fastapi_lifecycle_and_idempotence() -> None:
    runtime, output = make_runtime()
    unused, _ = make_runtime()
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return {"item": item_id}

    assert instrument_fastapi(app, runtime) is runtime
    assert instrument_fastapi(app, unused) is runtime
    with TestClient(app) as client:
        response = client.get(
            "/items/abc", headers={REQUEST_ID_HEADER: "fastapi-request"}
        )
    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == "fastapi-request"
    emitted = records(output)
    assert len(emitted) == 1
    assert emitted[0]["http.route"] == "/items/{item_id}"
    runtime.shutdown()
    unused.shutdown()


def test_fastapi_response_observability_failure_preserves_response(
    monkeypatch,
) -> None:
    runtime, output = make_runtime()
    app = FastAPI()

    @app.get("/ok")
    async def ok():
        return {"status": "ok"}

    def fail_response_headers():
        raise RuntimeError("response instrumentation failed")

    monkeypatch.setattr(runtime, "response_headers", fail_response_headers)
    instrument_fastapi(app, runtime)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/ok")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert REQUEST_ID_HEADER not in response.headers
    assert runtime.diagnostics.count("failure.fastapi.response_headers") == 1
    assert records(output)[0]["outcome"] == "success"
    runtime.shutdown()


def test_fastapi_error_and_non_http_scope() -> None:
    runtime, output = make_runtime()
    app = FastAPI()

    @app.get("/fail")
    async def fail():
        raise ValueError("route failed")

    instrument_fastapi(app, runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/fail")
    assert response.status_code == 500
    emitted = records(output)
    assert len(emitted) == 1
    assert emitted[0]["outcome"] == "error"
    runtime.shutdown()


def test_fastapi_raised_exception_takes_precedence_over_recorded_error() -> (
    None
):
    runtime, output = make_runtime()
    app = FastAPI()

    @app.get("/fail")
    async def fail():
        runtime.record_exception(
            LookupError("recorded by application"), handled=True
        )
        raise ValueError("route failed")

    instrument_fastapi(app, runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/fail")

    assert response.status_code == 500
    completions = _request_completions(records(output))
    assert len(completions) == 1
    completion = completions[0]
    assert completion["http.response.status_code"] == 500
    assert completion["outcome"] == "error"
    assert completion["error.type"] == "ValueError"
    assert completion["error.message"] == "route failed"
    runtime.shutdown()


def test_fastapi_handled_exception_does_not_mark_request_failed(
    monkeypatch,
) -> None:
    runtime, output = make_runtime()
    error_metric_points = _capture_error_metric_points(monkeypatch, runtime)
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def load_item(item_id: str):
        try:
            raise LookupError(f"item {item_id} is not cached")
        except LookupError as error:
            runtime.record_exception(error, handled=True)
        return {"item": item_id}

    instrument_fastapi(app, runtime)
    with TestClient(app) as client:
        response = client.get("/items/abc")

    assert response.status_code == 200
    completions = _request_completions(records(output))
    assert len(completions) == 1
    completion = completions[0]
    assert completion["outcome"] == "success"
    assert completion["severity"] == "INFO"
    assert _error_field_names(completion) == []
    assert [point["operation.kind"] for point in error_metric_points] == [
        "handled"
    ]
    runtime.shutdown()


def test_fastapi_exception_handler_error_is_kept_on_request_completion() -> (
    None
):
    runtime, output = make_runtime()
    app = FastAPI()

    class CalculationFailed(Exception):
        pass

    @app.exception_handler(CalculationFailed)
    async def handle_calculation_failure(_request, error):
        runtime.record_exception(error, handled=False, status_code=500)
        return JSONResponse({"error": "internal"}, status_code=500)

    @app.get("/fail")
    async def fail():
        raise CalculationFailed("calculation failed")

    instrument_fastapi(app, runtime)
    with TestClient(app) as client:
        response = client.get("/fail")

    assert response.status_code == 500
    completions = _request_completions(records(output))
    assert len(completions) == 1
    completion = completions[0]
    assert completion["outcome"] == "error"
    assert completion["error.type"] == "CalculationFailed"
    assert completion["error.message"] == "calculation failed"
    runtime.shutdown()


def test_fastapi_exception_recorded_in_endpoint_carries_matched_route() -> (
    None
):
    runtime, output = make_runtime()
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def load_item(item_id: str):
        try:
            raise LookupError(f"item {item_id} is not cached")
        except LookupError as error:
            runtime.record_exception(error, handled=True)
        return {"item": item_id}

    instrument_fastapi(app, runtime)
    with TestClient(app) as client:
        response = client.get("/items/abc")

    assert response.status_code == 200
    assert _routes_by_event_name(records(output)) == {
        "exception.recorded": ["/items/{item_id}"],
        "request.completed": ["/items/{item_id}"],
    }
    runtime.shutdown()


def test_fastapi_events_recorded_in_endpoints_carry_matched_routes() -> None:
    runtime, output = make_runtime()
    app = FastAPI()

    @app.get("/reports/{report_id}")
    async def build_report(report_id: str):
        runtime.event("report.built")
        return {"report": report_id}

    @app.get("/exports/{export_id}")
    def build_export(export_id: str):
        # FastAPI runs synchronous endpoints in a worker thread.
        runtime.event("export.built")
        return {"export": export_id}

    instrument_fastapi(app, runtime)
    with TestClient(app) as client:
        report_response = client.get("/reports/annual")
        export_response = client.get("/exports/annual")

    assert report_response.status_code == 200
    assert export_response.status_code == 200
    assert _routes_by_event_name(records(output)) == {
        "report.built": ["/reports/{report_id}"],
        "export.built": ["/exports/{export_id}"],
        "request.completed": [
            "/reports/{report_id}",
            "/exports/{export_id}",
        ],
    }
    runtime.shutdown()


def test_fastapi_request_matching_no_route_keeps_unmatched_route() -> None:
    runtime, output = make_runtime()
    app = FastAPI()

    @app.exception_handler(404)
    async def handle_not_found(_request, error):
        runtime.record_exception(error, handled=True, status_code=404)
        return JSONResponse({"detail": "not found"}, status_code=404)

    @app.get("/items/{item_id}")
    async def load_item(item_id: str):
        return {"item": item_id}

    instrument_fastapi(app, runtime)
    with TestClient(app) as client:
        response = client.get("/missing")

    assert response.status_code == 404
    assert _routes_by_event_name(records(output)) == {
        "exception.recorded": ["<unmatched>"],
        "request.completed": ["<unmatched>"],
    }
    runtime.shutdown()


def test_fastapi_record_written_before_routing_keeps_unmatched_route() -> None:
    runtime, output = make_runtime()
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def load_item(item_id: str):
        runtime.event("item.loaded")
        return {"item": item_id}

    # Middleware added before instrumentation runs inside the observability
    # middleware, so the request is active but has not been routed yet.
    @app.middleware("http")
    async def record_received_request(request, call_next):
        runtime.event("request.received")
        return await call_next(request)

    instrument_fastapi(app, runtime)
    with TestClient(app) as client:
        response = client.get("/items/abc")

    assert response.status_code == 200
    assert _routes_by_event_name(records(output)) == {
        "request.received": ["<unmatched>"],
        "item.loaded": ["/items/{item_id}"],
        "request.completed": ["/items/{item_id}"],
    }
    runtime.shutdown()


def test_httpx_only_instruments_supplied_sync_client() -> None:
    runtime, _output = make_runtime()
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    supplied = httpx.Client(transport=httpx.MockTransport(respond))
    untouched = httpx.Client(transport=httpx.MockTransport(respond))
    assert instrument_httpx(supplied, runtime) is supplied
    instrument_httpx(supplied, runtime)
    runtime.begin_request(
        headers={REQUEST_ID_HEADER: "outbound-request"},
        method="GET",
        route="/dispatch",
    )
    supplied.get("https://example.test/supplied")
    untouched.get("https://example.test/untouched")
    assert seen[0].headers[REQUEST_ID_HEADER] == "outbound-request"
    assert REQUEST_ID_HEADER not in seen[1].headers
    supplied.close()
    untouched.close()
    runtime.end_request(status_code=200)
    runtime.shutdown()


def test_httpx_async_client_and_invalid_client_are_nonfatal() -> None:
    runtime, _output = make_runtime()
    seen: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async def run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        instrument_httpx(client, runtime)
        runtime.begin_request(
            headers={REQUEST_ID_HEADER: "async-request"},
            method="POST",
            route="/dispatch",
        )
        await client.get("https://example.test/async")
        await client.aclose()
        runtime.end_request(status_code=200)

    asyncio.run(run())
    assert seen[0].headers[REQUEST_ID_HEADER] == "async-request"
    marker = object()
    assert instrument_httpx(marker, runtime) is marker
    assert runtime.diagnostics.count("failure.httpx.instrument") == 1
    runtime.shutdown()


def test_httpx_client_cannot_be_rebound_to_another_runtime() -> None:
    first, _ = make_runtime()
    second, _ = make_runtime()
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    )
    instrument_httpx(client, first)
    instrument_httpx(client, second)
    assert second.diagnostics.count("failure.httpx.already_instrumented") == 1
    client.close()
    first.shutdown()
    second.shutdown()


def _capture_error_metric_points(monkeypatch, runtime) -> list[dict[str, Any]]:
    """Record the attributes of every error-count metric point."""

    error_metric_points: list[dict[str, Any]] = []
    record_error = runtime._otel.record_error

    def record_and_capture_error(attributes):
        error_metric_points.append(dict(attributes))
        record_error(attributes)

    monkeypatch.setattr(
        runtime._otel, "record_error", record_and_capture_error
    )
    return error_metric_points


def _request_completions(emitted: list[dict]) -> list[dict]:
    return [
        item for item in emitted if item["event.name"] == "request.completed"
    ]


def _routes_by_event_name(emitted: list[dict]) -> dict[str, list[str]]:
    """Group each record's route under its event name, in emission order."""

    routes: dict[str, list[str]] = {}
    for item in emitted:
        routes.setdefault(item["event.name"], []).append(item["http.route"])
    return routes


def _error_field_names(record: dict) -> list[str]:
    return sorted(key for key in record if key.startswith("error."))


def _flask_callback_snapshot(
    app: Flask,
) -> dict[str, dict[Any, tuple[Any, ...]]]:
    return {
        name: {
            key: tuple(callbacks)
            for key, callbacks in getattr(app, name).items()
        }
        for name in (
            "before_request_funcs",
            "after_request_funcs",
            "teardown_request_funcs",
        )
    }
