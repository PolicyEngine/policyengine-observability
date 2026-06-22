from __future__ import annotations

import asyncio
from enum import StrEnum

import pytest

from policyengine_observability import ObservabilityConfig
from policyengine_observability import ObservabilityRuntime
from policyengine_observability.adapters.fastapi import UNMATCHED_ROUTE
from policyengine_observability.adapters.fastapi import (
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
