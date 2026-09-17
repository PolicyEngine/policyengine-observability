from __future__ import annotations

import json
import math

import pytest

from policyengine_observability.config import ObservabilityConfig
from policyengine_observability.destinations import (
    GoogleCloudLoggingDestination,
    google_cloud_logging,
    normalize_payload,
)
from policyengine_observability.destinations.base import (
    accepts_keyword,
    clamped,
)


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("cannot stringify")


class FakeLogger:
    def __init__(self) -> None:
        self.calls = []

    def log_struct(self, payload, **kwargs) -> None:
        self.calls.append((payload, kwargs))


class FakeGapicApi:
    def __init__(self) -> None:
        self.calls = []

    def write_log_entries(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


class FakeLoggingApi:
    def __init__(self) -> None:
        self._gapic_api = FakeGapicApi()


class FakeClient:
    def __init__(self, *, gapic: bool = False) -> None:
        self.project = "resolved-project"
        self.fake_logger = FakeLogger()
        self.log_names = []
        if gapic:
            self.logging_api = FakeLoggingApi()

    def logger(self, log_name: str) -> FakeLogger:
        self.log_names.append(log_name)
        return self.fake_logger


def _google_destination(monkeypatch, client, **kwargs):
    monkeypatch.setattr(
        google_cloud_logging,
        "load_google_credentials",
        lambda *, prefer_workload_identity: None,
    )
    monkeypatch.setattr(
        google_cloud_logging,
        "configure_google_application_credentials",
        lambda: None,
    )
    return GoogleCloudLoggingDestination(
        project=None,
        log_name="policyengine-observability",
        client_factory=lambda _project, _credentials: client,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5.0, 5.0),
        ("5", 5.0),
        (0, 0.5),
        (-3, 0.5),
        (1000, 60.0),
        (float("inf"), 10.0),
        (float("nan"), 10.0),
        (None, 10.0),
        ("garbage", 10.0),
    ],
)
def test_clamped_bounds_and_rejects_non_finite(value, expected) -> None:
    result = clamped(value, low=0.5, high=60.0, default=10.0)

    assert result == expected
    assert math.isfinite(result)


def test_accepts_keyword_covers_named_var_keyword_and_uninspectable() -> None:
    def named(payload, *, timestamp=None):
        pass

    def var_keyword(payload, **kwargs):
        pass

    def blind(payload):
        pass

    assert accepts_keyword(named, "timestamp") is True
    assert accepts_keyword(var_keyword, "timestamp") is True
    assert accepts_keyword(blind, "timestamp") is False
    # Builtins without introspectable signatures degrade to False
    # instead of raising at construction time.
    assert accepts_keyword(min, "timestamp") is False


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


def test_google_destination_writes_structured_log_with_bounded_labels(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        google_cloud_logging,
        "load_google_credentials",
        lambda *, prefer_workload_identity: None,
    )
    monkeypatch.setattr(
        google_cloud_logging,
        "configure_google_application_credentials",
        lambda: None,
    )
    client = FakeClient()
    destination = GoogleCloudLoggingDestination(
        project=None,
        log_name="policyengine-observability",
        client_factory=lambda _project, _credentials: client,
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


def test_google_destination_bounds_gapic_writes(monkeypatch) -> None:
    client = FakeClient(gapic=True)

    destination = _google_destination(
        monkeypatch, client, write_timeout_seconds=5.0
    )
    # The write path under log_struct funnels through this method; the
    # rebinding must inject the bounded retry and per-call timeout.
    client.logging_api._gapic_api.write_log_entries(request="sentinel")

    assert destination.write_timeout_seconds == 5.0
    ((args, kwargs),) = client.logging_api._gapic_api.calls
    assert kwargs["request"] == "sentinel"
    assert kwargs["timeout"] == 5.0
    assert kwargs["retry"].timeout == 5.0


def test_google_destination_clamps_write_timeout(monkeypatch) -> None:
    destination = _google_destination(
        monkeypatch, FakeClient(gapic=True), write_timeout_seconds=0.0
    )

    assert destination.write_timeout_seconds == 0.5


def test_google_destination_without_gapic_transport_still_works(
    monkeypatch,
) -> None:
    client = FakeClient()

    destination = _google_destination(monkeypatch, client)
    destination.emit({"event": "x"}, log_type="event", severity="INFO")

    assert len(client.fake_logger.calls) == 1


def test_google_destination_forwards_enqueue_timestamp(monkeypatch) -> None:
    from datetime import UTC, datetime

    client = FakeClient()
    destination = _google_destination(monkeypatch, client)
    stamp = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)

    destination.emit(
        {"event": "x"}, log_type="event", severity="INFO", timestamp=stamp
    )
    destination.emit({"event": "y"}, log_type="event", severity="INFO")

    (_, stamped_kwargs), (_, plain_kwargs) = client.fake_logger.calls
    assert stamped_kwargs["timestamp"] is stamp
    assert "timestamp" not in plain_kwargs


def test_google_destination_close_closes_client(monkeypatch) -> None:
    class ClosableFakeClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    client = ClosableFakeClient()
    destination = _google_destination(monkeypatch, client)

    destination.close()

    assert client.closed == 1


def test_google_destination_close_tolerates_closeless_client(
    monkeypatch,
) -> None:
    destination = _google_destination(monkeypatch, FakeClient())

    destination.close()  # FakeClient has no close; must be a no-op


def test_google_destination_suppresses_instrumentation_entry(
    monkeypatch,
) -> None:
    logging_v2 = pytest.importorskip("google.cloud.logging_v2")
    monkeypatch.setattr(
        logging_v2, "_instrumentation_emitted", False, raising=False
    )

    _google_destination(monkeypatch, FakeClient())

    assert logging_v2._instrumentation_emitted is True


def test_google_factory_reads_write_timeout_env(monkeypatch) -> None:
    captured = {}

    class StubDestination:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        google_cloud_logging, "GoogleCloudLoggingDestination", StubDestination
    )
    monkeypatch.setenv("OBSERVABILITY_GOOGLE_WRITE_TIMEOUT_SECONDS", "2.5")

    from policyengine_observability.destinations.registry import (
        destination_strategy,
    )

    destination_strategy("google_cloud_logging").factory(
        config=ObservabilityConfig(google_cloud_project="proj"),
        loggers={},
        serializer=json.dumps,
    )

    assert captured["project"] == "proj"
    assert captured["write_timeout_seconds"] == 2.5


def test_google_factory_write_timeout_defaults_without_env(
    monkeypatch,
) -> None:
    captured = {}

    class StubDestination:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        google_cloud_logging, "GoogleCloudLoggingDestination", StubDestination
    )
    monkeypatch.delenv(
        "OBSERVABILITY_GOOGLE_WRITE_TIMEOUT_SECONDS", raising=False
    )

    from policyengine_observability.destinations.registry import (
        destination_strategy,
    )

    destination_strategy("google_cloud_logging").factory(
        config=ObservabilityConfig(google_cloud_project="proj"),
        loggers={},
        serializer=json.dumps,
    )

    assert captured["write_timeout_seconds"] == 10.0


# ── Stdout formatters ────────────────────────────────────────────────────
