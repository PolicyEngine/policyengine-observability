from __future__ import annotations

import asyncio

import httpx
from conftest import make_runtime, records
from fastapi import FastAPI
from fastapi.testclient import TestClient
from flask import Flask

from policyengine_observability import (
    REQUEST_ID_HEADER,
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
    runtime, output = make_runtime()
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
