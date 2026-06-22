from __future__ import annotations

import pytest

from policyengine_observability import (
    ObservabilityConfig,
    ObservabilityRuntime,
)
from policyengine_observability.adapters.flask import (
    FlaskObservabilityAdapter,
    _split_forwarded_for,
    init_flask_observability,
)


class RecordingInstrument:
    def __init__(self) -> None:
        self.calls = []

    def add(self, value, attributes=None) -> None:
        self.calls.append(("add", value, attributes))

    def record(self, value, attributes=None) -> None:
        self.calls.append(("record", value, attributes))


def _flask():
    return pytest.importorskip("flask")


def test_flask_adapter_records_request_metadata_and_headers() -> None:
    flask = _flask()
    app = flask.Flask(__name__)
    runtime = ObservabilityRuntime(
        ObservabilityConfig(
            service_name="svc",
            metric_attribute_keys=(
                "service.name",
                "route",
                "method",
                "status_code",
                "segment",
                "tool",
            ),
        )
    )
    runtime.requests = RecordingInstrument()
    runtime.http_duration = RecordingInstrument()
    runtime.active_requests = RecordingInstrument()
    runtime.segment_duration = RecordingInstrument()

    @app.get("/items/<item_id>")
    def item(item_id: str):
        runtime.set_attribute("tool", "handler")
        with runtime.segment("load", tool="handler"):
            pass
        return {"item_id": item_id}

    initialized = init_flask_observability(
        app,
        runtime=runtime,
        service_name="svc",
    )

    response = app.test_client().get(
        "/items/abc?debug=1",
        headers={
            "X-Forwarded-For": "203.0.113.1, 198.51.100.2",
            "User-Agent": "pytest",
            "Origin": "https://policyengine.org",
        },
    )

    assert initialized is runtime
    assert response.status_code == 200
    assert response.headers["X-PolicyEngine-Request-Id"]
    _, _, request_attributes = runtime.requests.calls[0]
    _, _, segment_attributes = runtime.segment_duration.calls[0]
    assert request_attributes["route"] == "/items/<item_id>"
    assert request_attributes["status_code"] == "200"
    assert segment_attributes["tool"] == "handler"
    assert runtime.active_requests.calls[0][1] == 1
    assert runtime.active_requests.calls[-1][1] == -1


def test_flask_adapter_is_idempotent_and_honors_disabled_runtime() -> None:
    flask = _flask()
    app = flask.Flask(__name__)
    runtime = ObservabilityRuntime.disabled()

    first = init_flask_observability(
        app,
        runtime=runtime,
        service_name="svc",
    )
    second = init_flask_observability(
        app,
        runtime=ObservabilityRuntime(
            ObservabilityConfig(service_name="other")
        ),
        service_name="other",
    )

    assert first is runtime
    assert second is runtime
    assert "policyengine_observability_adapter" not in app.extensions


def test_flask_adapter_enabled_idempotence_and_start_failure() -> None:
    flask = _flask()
    app = flask.Flask(__name__)
    runtime = ObservabilityRuntime(ObservabilityConfig(service_name="svc"))
    adapter = FlaskObservabilityAdapter(runtime)
    failures = []
    runtime.log_observability_failure = lambda operation, exc, **fields: (
        failures.append(operation)
    )

    adapter.instrument_app(app)
    adapter.instrument_app(app)
    adapter.start_request()

    assert app.extensions["policyengine_observability_adapter"] is adapter
    assert failures == ["flask.before_request"]


def test_flask_init_builds_runtime_from_config_and_returns_existing() -> None:
    flask = _flask()
    app = flask.Flask(__name__)

    first = init_flask_observability(
        app,
        config=ObservabilityConfig(service_name="svc", enabled=False),
        service_name="ignored",
    )
    second = init_flask_observability(app, service_name="other")

    assert first is second
    assert first.config.service_name == "svc"


def test_flask_inbound_metadata_ip_source_variants() -> None:
    flask = _flask()
    app = flask.Flask(__name__)
    runtime = ObservabilityRuntime(
        ObservabilityConfig(service_name="svc", log_raw_ip=False)
    )
    adapter = FlaskObservabilityAdapter(runtime)

    with app.test_request_context(
        "/",
        headers={"X-Real-IP": "198.51.100.5"},
    ):
        metadata = adapter._inbound_metadata(flask.request)
        assert metadata["ip_source"] == "x_real_ip"
        assert "client_ip" not in metadata

    with app.test_request_context(
        "/", environ_base={"REMOTE_ADDR": "10.0.0.1"}
    ):
        metadata = adapter._inbound_metadata(flask.request)
        assert metadata["ip_source"] == "remote_addr"


def test_split_forwarded_for_discards_empty_parts() -> None:
    assert _split_forwarded_for(None) == []
    assert _split_forwarded_for(" 1.1.1.1, ,2.2.2.2 ") == [
        "1.1.1.1",
        "2.2.2.2",
    ]
