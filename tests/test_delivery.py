from __future__ import annotations

import io
import json
import threading
import time

from conftest import make_config

from policyengine_observability import (
    DeploymentIdentity,
    GoogleCloudLoggingConfig,
    LoggingConfig,
)
from policyengine_observability.delivery import (
    DeliveryManager,
    _GoogleCloudWriter,
)
from policyengine_observability.diagnostics import Diagnostics


class RecordingWriter:
    def __init__(self) -> None:
        self.records: list[dict] = []
        self.closed = False
        self.written = threading.Event()

    def write(self, record: dict) -> None:
        self.records.append(record)
        self.written.set()

    def write_many(self, records: list[dict]) -> None:
        self.records.extend(records)
        self.written.set()

    def close(self) -> None:
        self.closed = True


def modal_config(**remote_overrides):
    remote_values = {
        "project_id": "policyengine-observability",
        "queue_capacity": 10,
        "batch_size": 5,
    }
    shutdown_timeout_seconds = remote_overrides.pop(
        "shutdown_timeout_seconds", 0.2
    )
    remote_values.update(remote_overrides)
    return make_config(
        deployment=DeploymentIdentity("test", "modal"),
        logging=LoggingConfig(
            stdout_enabled=True,
            remote=GoogleCloudLoggingConfig(**remote_values),
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        ),
    )


def test_stdout_delivery_is_single_line_json() -> None:
    output = io.StringIO()
    manager = DeliveryManager(make_config(), Diagnostics(), stdout=output)
    manager.emit({"severity": "INFO", "event.name": "test"})
    assert output.getvalue().count("\n") == 1
    assert json.loads(output.getvalue())["event.name"] == "test"
    assert not manager.remote_enabled


def test_modal_delivery_writes_stdout_and_uses_lazy_worker() -> None:
    output = io.StringIO()
    diagnostics = Diagnostics()
    writer = RecordingWriter()
    factory_calls = 0

    def factory(_config):
        nonlocal factory_calls
        factory_calls += 1
        return writer

    manager = DeliveryManager(
        modal_config(),
        diagnostics,
        stdout=output,
        writer_factory=factory,
    )
    assert factory_calls == 0
    manager.emit({"severity": "INFO", "event.name": "modal"})
    assert json.loads(output.getvalue())["event.name"] == "modal"
    assert writer.written.wait(1)
    assert writer.records[0]["event.name"] == "modal"
    assert factory_calls == 1
    manager.close(1)
    assert writer.closed


def test_cloud_run_never_creates_direct_writer() -> None:
    calls = 0

    def factory(_config):
        nonlocal calls
        calls += 1
        return RecordingWriter()

    manager = DeliveryManager(
        make_config(
            deployment=DeploymentIdentity("prod", "google_cloud_run"),
            logging=LoggingConfig(
                remote=GoogleCloudLoggingConfig(project_id="central")
            ),
        ),
        Diagnostics(),
        stdout=io.StringIO(),
        writer_factory=factory,
    )
    manager.emit({"event.name": "cloud-run"})
    manager.close()
    assert calls == 0
    assert not manager.remote_enabled


def test_queue_saturation_drops_without_blocking() -> None:
    started = threading.Event()
    release = threading.Event()
    diagnostics = Diagnostics()

    class BlockingWriter(RecordingWriter):
        def write_many(self, records: list[dict]) -> None:
            started.set()
            release.wait(1)
            super().write_many(records)

    writer = BlockingWriter()
    manager = DeliveryManager(
        modal_config(queue_capacity=1, batch_size=1),
        diagnostics,
        stdout=io.StringIO(),
        writer_factory=lambda _config: writer,
    )
    manager.emit({"sequence": 1})
    assert started.wait(1)
    manager.emit({"sequence": 2})
    before = time.perf_counter()
    manager.emit({"sequence": 3})
    assert time.perf_counter() - before < 0.1
    assert diagnostics.count("logs.dropped.queue_full") == 1
    release.set()
    manager.close(1)


def test_writer_auth_or_permission_failure_is_nonfatal() -> None:
    diagnostics = Diagnostics()
    attempted = threading.Event()

    def unavailable(_config):
        attempted.set()
        raise PermissionError("denied")

    manager = DeliveryManager(
        modal_config(),
        diagnostics,
        stdout=io.StringIO(),
        writer_factory=unavailable,
    )
    manager.emit({"event.name": "survives"})
    assert attempted.wait(1)
    deadline = time.time() + 1
    while diagnostics.count("logs.export_failure") == 0:
        assert time.time() < deadline
        time.sleep(0.01)
    manager.close(1)
    assert diagnostics.count("logs.export_failure") == 1


def test_malformed_record_and_closed_queue_are_nonfatal() -> None:
    diagnostics = Diagnostics()
    output = io.StringIO()
    manager = DeliveryManager(make_config(), diagnostics, stdout=output)
    manager.emit({"bad": object()})
    assert diagnostics.count("failure.stdout.write") == 1

    remote = DeliveryManager(
        modal_config(),
        diagnostics,
        stdout=io.StringIO(),
        writer_factory=lambda _config: RecordingWriter(),
    )
    remote.close(1)
    remote.emit({"event.name": "after-close"})
    assert diagnostics.count("logs.dropped.closed") == 1


def test_shutdown_timeout_is_bounded_and_repeatable() -> None:
    started = threading.Event()
    release = threading.Event()
    diagnostics = Diagnostics()

    class BlockingWriter(RecordingWriter):
        def write_many(self, records: list[dict]) -> None:
            started.set()
            release.wait(1)

    manager = DeliveryManager(
        modal_config(shutdown_timeout_seconds=0.01),
        diagnostics,
        stdout=io.StringIO(),
        writer_factory=lambda _config: BlockingWriter(),
    )
    manager.emit({"event.name": "block"})
    assert started.wait(1)
    before = time.perf_counter()
    manager.close(0.01)
    manager.close(0.01)
    assert time.perf_counter() - before < 0.2
    assert diagnostics.count("logs.shutdown_timeout") == 1
    release.set()


def test_google_writer_batches_with_bounded_api_calls(monkeypatch) -> None:
    from google.cloud import logging_v2

    committed: list[dict] = []

    class Batch:
        def log_struct(self, record, **kwargs):
            committed.append({"record": record, "kwargs": kwargs})

        def commit(self):
            committed.append({"committed": True})

    class Logger:
        def batch(self):
            return Batch()

    class Gapic:
        def write_log_entries(self, *args, **kwargs):
            return None

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.logging_api = type("API", (), {"_gapic_api": Gapic()})()
            self.closed = False

        def logger(self, name):
            assert name == "policyengine-api-v1-modal"
            return Logger()

        def close(self):
            self.closed = True

    monkeypatch.setattr(logging_v2, "Client", Client)
    monkeypatch.setattr(
        "policyengine_observability.google_credentials.load_google_credentials",
        lambda **_kwargs: "credentials",
    )
    writer = _GoogleCloudWriter(
        GoogleCloudLoggingConfig(project_id="central", write_timeout_seconds=2)
    )
    writer.write_many(
        [
            {
                "severity": "WARNING",
                "logging.googleapis.com/trace": "projects/central/traces/a",
                "logging.googleapis.com/spanId": "b",
                "logging.googleapis.com/trace_sampled": True,
            },
            {"severity": "INFO"},
        ]
    )
    assert committed[-1] == {"committed": True}
    assert committed[0]["kwargs"] == {
        "severity": "WARNING",
        "trace": "projects/central/traces/a",
        "span_id": "b",
        "trace_sampled": True,
    }
    assert committed[1]["kwargs"]["trace_sampled"] is False
    writer.close()
    assert writer._client.closed
