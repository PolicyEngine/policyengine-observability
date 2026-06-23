from __future__ import annotations

import asyncio
from enum import StrEnum

import pytest

from policyengine_observability import (
    ObservabilityConfig,
    ObservabilityRuntime,
)
from policyengine_observability.adapters.fastapi import (
    UNMATCHED_ROUTE,
    FastAPIObservabilityAdapter,
    FastAPIObservabilityMiddleware,
    _endpoint_from_scope,
    _headers_from_scope,
    _int_header,
    _merge_response_headers,
    _query_keys,
    _route_from_scope,
    _split_forwarded_for,
    init_fastapi_observability,
)


class SegmentName(StrEnum):
    LOAD = "load"


class RecordingInstrument:
    def __init__(self) -> None:
        self.calls = []

    def add(self, value, attributes=None) -> None:
        self.calls.append(("add", value, attributes))

    def record(self, value, attributes=None) -> None:
        self.calls.append(("record", value, attributes))


def _fastapi_modules():
    fastapi = pytest.importorskip("fastapi")
    responses = pytest.importorskip("starlette.responses")
    return fastapi, responses


def _call_asgi(app, path: str):
    async def run():
        messages = []
        received_request = False
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "root_path": "",
        }

        async def receive():
            nonlocal received_request
            if not received_request:
                received_request = True
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            await asyncio.sleep(60)
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        return messages

    return asyncio.run(run())


def test_fastapi_streaming_request_finishes_after_final_body() -> None:
    fastapi, responses = _fastapi_modules()
    app = fastapi.FastAPI()
    observed = ObservabilityRuntime(
        ObservabilityConfig(
            service_name="svc",
            metric_attribute_keys=(
                "service.name",
                "route",
                "method",
                "segment",
                "tool",
            ),
        ),
        segment_registry=SegmentName,
    )
    observed.requests = RecordingInstrument()
    observed.http_duration = RecordingInstrument()
    observed.segment_duration = RecordingInstrument()
    observed.active_requests = RecordingInstrument()

    @app.get("/items/{item_id}")
    async def get_item(item_id: str):
        async def body():
            with observed.segment(SegmentName.LOAD, tool="stream"):
                yield f"item:{item_id}".encode()

        return responses.StreamingResponse(body())

    init_fastapi_observability(
        app,
        runtime=observed,
        service_name="svc",
    )

    messages = _call_asgi(app, "/items/abc")
    response_start = next(
        message
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = dict(response_start["headers"])

    assert body == b"item:abc"
    assert response_headers[b"X-PolicyEngine-Request-Id"]
    _, _, request_attributes = observed.requests.calls[0]
    _, _, segment_attributes = observed.segment_duration.calls[0]
    assert request_attributes["route"] == "/items/{item_id}"
    assert segment_attributes["route"] == "/items/{item_id}"
    assert segment_attributes["tool"] == "stream"
    assert observed.active_requests.calls[0][1] == 1
    assert observed.active_requests.calls[-1][1] == -1


def test_fastapi_unmatched_route_uses_stable_metric_label() -> None:
    fastapi, _responses = _fastapi_modules()
    app = fastapi.FastAPI()
    observed = ObservabilityRuntime(ObservabilityConfig(service_name="svc"))
    observed.requests = RecordingInstrument()
    observed.http_duration = RecordingInstrument()
    observed.active_requests = RecordingInstrument()

    init_fastapi_observability(
        app,
        runtime=observed,
        service_name="svc",
    )

    messages = _call_asgi(app, "/missing/abc")
    response_start = next(
        message
        for message in messages
        if message["type"] == "http.response.start"
    )

    assert response_start["status"] == 404
    _, _, request_attributes = observed.requests.calls[0]
    assert request_attributes["route"] == UNMATCHED_ROUTE


def test_fastapi_static_attributes_are_recorded() -> None:
    fastapi, _responses = _fastapi_modules()
    app = fastapi.FastAPI()
    observed = ObservabilityRuntime(
        ObservabilityConfig(
            service_name="svc",
            metric_attribute_keys=(
                "service.name",
                "route",
                "method",
                "platform",
                "runtime_role",
            ),
        )
    )
    observed.requests = RecordingInstrument()
    observed.http_duration = RecordingInstrument()
    observed.active_requests = RecordingInstrument()

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    init_fastapi_observability(
        app,
        runtime=observed,
        service_name="svc",
        static_attributes={
            "platform": "modal",
            "runtime_role": "modal_web",
            "ignored": None,
        },
    )

    _call_asgi(app, "/ok")

    _, _, request_attributes = observed.requests.calls[0]
    assert request_attributes["platform"] == "modal"
    assert request_attributes["runtime_role"] == "modal_web"
    assert "ignored" not in request_attributes


def test_fastapi_adapter_disabled_and_idempotent_paths() -> None:
    fastapi, _responses = _fastapi_modules()
    app = fastapi.FastAPI()
    disabled = ObservabilityRuntime.disabled()
    adapter = FastAPIObservabilityAdapter(disabled)

    adapter.instrument_app(app)

    assert not hasattr(app.state, "policyengine_observability_adapter")

    observed = ObservabilityRuntime(
        ObservabilityConfig(service_name="svc", instrument_fastapi=True)
    )
    adapter = FastAPIObservabilityAdapter(observed)
    adapter.instrument_app(app)
    adapter.instrument_app(app)

    assert app.state.policyengine_observability_adapter is adapter


def test_fastapi_adapter_logs_middleware_and_start_failures() -> None:
    class BrokenApp:
        state = type("State", (), {})()

        def add_middleware(self, *_args, **_kwargs):
            raise RuntimeError("middleware failed")

    observed = ObservabilityRuntime(ObservabilityConfig(service_name="svc"))
    failures = []
    observed.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )
    adapter = FastAPIObservabilityAdapter(observed)
    adapter.instrument_app(BrokenApp())
    observed.begin_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("begin failed")
    )
    adapter.start_request({"headers": [], "path": "/broken"})

    assert failures == [
        "fastapi.middleware_install",
        "fastapi.before_request",
    ]


def test_fastapi_init_returns_existing_runtime() -> None:
    fastapi, _responses = _fastapi_modules()
    app = fastapi.FastAPI()
    runtime = ObservabilityRuntime.disabled()
    app.state.policyengine_observability = runtime

    assert init_fastapi_observability(app, service_name="svc") is runtime


def test_fastapi_inbound_metadata_ip_source_variants() -> None:
    observed = ObservabilityRuntime(
        ObservabilityConfig(service_name="svc", log_raw_ip=False)
    )
    adapter = FastAPIObservabilityAdapter(observed)

    real_ip = adapter._inbound_metadata(
        {"client": ("10.0.0.1", 123)},
        {"x-real-ip": "198.51.100.5"},
    )
    remote_addr = adapter._inbound_metadata(
        {"client": ("10.0.0.1", 123)},
        {},
    )

    assert real_ip["ip_source"] == "x_real_ip"
    assert remote_addr["ip_source"] == "remote_addr"
    assert "client_ip" not in real_ip


def test_fastapi_middleware_non_http_scope_passthrough() -> None:
    calls = []

    async def app(scope, _receive, _send):
        calls.append(scope["type"])

    adapter = FastAPIObservabilityAdapter(
        ObservabilityRuntime(ObservabilityConfig(service_name="svc"))
    )
    middleware = FastAPIObservabilityMiddleware(app, adapter=adapter)

    async def run():
        await middleware({"type": "lifespan"}, None, None)

    asyncio.run(run())

    assert calls == ["lifespan"]


def test_fastapi_middleware_records_exception_before_reraising() -> None:
    observed = ObservabilityRuntime(ObservabilityConfig(service_name="svc"))
    observed.errors = RecordingInstrument()
    observed.requests = RecordingInstrument()
    observed.http_duration = RecordingInstrument()
    observed.active_requests = RecordingInstrument()

    async def app(_scope, _receive, _send):
        raise RuntimeError("app failed")

    adapter = FastAPIObservabilityAdapter(observed)
    middleware = FastAPIObservabilityMiddleware(app, adapter=adapter)

    async def run():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/boom",
            "query_string": b"a=1",
            "headers": [],
            "client": ("127.0.0.1", 123),
        }
        with pytest.raises(RuntimeError, match="app failed"):
            await middleware(scope, receive, send)

    asyncio.run(run())

    assert observed.errors.calls[0][0] == "add"
    assert observed.requests.calls[0][0] == "add"


def test_fastapi_helpers_handle_edge_inputs() -> None:
    scope = {
        "headers": [
            (b"x-test", b"one"),
            (b"x-test", b"two"),
            (object(), object()),
        ],
        "query_string": "a=1&b=&a=2",
    }

    assert _headers_from_scope(scope)["x-test"] == "one,two"
    assert _query_keys(scope) == ["a", "b"]
    assert _route_from_scope({"route": object()}) is None
    assert _endpoint_from_scope({"endpoint": "callable-ish"}) == "callable-ish"
    assert _int_header("bad") is None
    assert _int_header("12") == 12
    assert _split_forwarded_for("1.1.1.1, ,2.2.2.2") == [
        "1.1.1.1",
        "2.2.2.2",
    ]
    assert _merge_response_headers(
        [(b"x-policyengine-request-id", b"old"), (b"x-other", b"keep")],
        {
            "X-PolicyEngine-Request-Id": "new",
            "traceparent": "parent",
            "ignored": "value",
        },
    ) == [
        (b"x-other", b"keep"),
        (b"X-PolicyEngine-Request-Id", b"new"),
        (b"traceparent", b"parent"),
    ]
