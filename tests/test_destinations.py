from __future__ import annotations

from policyengine_observability.destinations import (
    GoogleCloudLoggingDestination,
    normalize_payload,
)


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("cannot stringify")


class FakeLogger:
    def __init__(self) -> None:
        self.calls = []

    def log_struct(self, payload, **kwargs) -> None:
        self.calls.append((payload, kwargs))


class FakeClient:
    def __init__(self) -> None:
        self.project = "resolved-project"
        self.fake_logger = FakeLogger()
        self.log_names = []

    def logger(self, log_name: str) -> FakeLogger:
        self.log_names.append(log_name)
        return self.fake_logger


def test_normalize_payload_recursively_stringifies_unsafe_values() -> None:
    normalized = normalize_payload(
        {
            "keep": "value",
            "drop_none": None,
            "bytes": b"value",
            "list": [1, Unprintable()],
            "nested": {"object": object()},
        }
    )

    assert normalized["keep"] == "value"
    assert normalized["drop_none"] is None
    assert normalized["bytes"] == "value"
    assert normalized["list"] == [1, "<unprintable Unprintable>"]
    assert normalized["nested"]["object"].startswith("<object object at ")


def test_google_destination_writes_structured_log_with_bounded_labels() -> (
    None
):
    client = FakeClient()
    destination = GoogleCloudLoggingDestination(
        project=None,
        log_name="policyengine-observability",
        client_factory=lambda _project: client,
    )

    destination.emit(
        {
            "schema_version": "policyengine.observability.request.v1",
            "service_name": "svc",
            "service_role": "api",
            "environment": "production",
            "request_id": "request-1",
            "trace_id": "abc123",
            "span_id": "def456",
            "path": "/calculate",
            "object": object(),
        },
        log_type="request",
        severity="ERROR",
    )

    payload, kwargs = client.fake_logger.calls[0]
    assert client.log_names == ["policyengine-observability"]
    assert payload["object"].startswith("<object object at ")
    assert kwargs["severity"] == "ERROR"
    assert kwargs["trace"] == "projects/resolved-project/traces/abc123"
    assert kwargs["span_id"] == "def456"
    assert kwargs["labels"] == {
        "log_type": "request",
        "service_name": "svc",
        "service_role": "api",
        "environment": "production",
        "schema_version": "policyengine.observability.request.v1",
    }
    assert "request_id" not in kwargs["labels"]
    assert "path" not in kwargs["labels"]
