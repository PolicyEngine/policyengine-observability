from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from conftest import make_runtime, records
from fastapi import FastAPI
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
