from __future__ import annotations

import io
import json
import threading
import time

from conftest import make_config

from policyengine_observability import (
    CustomLogDestination,
    DeploymentIdentity,
    GoogleCloudLogDestination,
    LoggingConfig,
    StdoutLogDestination,
)
from policyengine_observability.delivery import DeliveryManager
from policyengine_observability.destinations.google_cloud import (
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


def queued_config(writer_factory, **overrides):
    queue_capacity = overrides.pop("queue_capacity", 10)
    batch_size = overrides.pop("batch_size", 5)
    shutdown_timeout_seconds = overrides.pop("shutdown_timeout_seconds", 0.2)
    platform = overrides.pop("platform", "modal")
    assert not overrides
    return make_config(
        deployment=DeploymentIdentity("test", platform),
        logging=LoggingConfig(
            destinations=(
                StdoutLogDestination(),
                CustomLogDestination(
                    name="recording",
                    writer_factory=writer_factory,
                    delivery="queued",
                    queue_capacity=queue_capacity,
                    batch_size=batch_size,
                ),
            ),
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


def test_queued_delivery_writes_stdout_and_uses_lazy_worker() -> None:
    output = io.StringIO()
    diagnostics = Diagnostics()
    writer = RecordingWriter()
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return writer

    manager = DeliveryManager(
        queued_config(factory),
        diagnostics,
        stdout=output,
    )
    assert factory_calls == 0
    manager.emit({"severity": "INFO", "event.name": "queued"})
    assert json.loads(output.getvalue())["event.name"] == "queued"
    assert writer.written.wait(1)
    assert writer.records[0]["event.name"] == "queued"
    assert factory_calls == 1
    manager.close(1)
    assert writer.closed


def test_explicit_queued_destination_is_platform_independent() -> None:
    writer = RecordingWriter()
    manager = DeliveryManager(
        queued_config(lambda: writer, platform="google_cloud_run"),
        Diagnostics(),
        stdout=io.StringIO(),
    )
    manager.emit({"event.name": "cloud-run"})
    assert writer.written.wait(1)
    manager.close(1)


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
        queued_config(
            lambda: writer,
            queue_capacity=1,
            batch_size=1,
        ),
        diagnostics,
        stdout=io.StringIO(),
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

    def unavailable():
        attempted.set()
        raise PermissionError("denied")

    manager = DeliveryManager(
        queued_config(unavailable),
        diagnostics,
        stdout=io.StringIO(),
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
    manager = DeliveryManager(make_config(), diagnostics, stdout=io.StringIO())
    manager.emit({"bad": object()})
    assert diagnostics.count("logs.export_failure") == 1

    remote = DeliveryManager(
        queued_config(lambda: RecordingWriter()),
        diagnostics,
        stdout=io.StringIO(),
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
        queued_config(
            lambda: BlockingWriter(),
            shutdown_timeout_seconds=0.01,
        ),
        diagnostics,
        stdout=io.StringIO(),
    )
    manager.emit({"event.name": "block"})
    assert started.wait(1)
    before = time.perf_counter()
    manager.close(0.01)
    manager.close(0.01)
    assert time.perf_counter() - before < 0.2
    assert diagnostics.count("logs.shutdown_timeout") == 1
    release.set()


def test_inline_failure_cleanup_does_not_block_emit() -> None:
    close_started = threading.Event()
    release = threading.Event()

    class FailingWriter:
        def write(self, _record: dict) -> None:
            raise RuntimeError("write failed")

        def close(self) -> None:
            close_started.set()
            release.wait(1)

    manager = DeliveryManager(
        make_config(
            logging=LoggingConfig(
                destinations=(
                    CustomLogDestination(
                        name="failing-inline",
                        writer_factory=FailingWriter,
                        delivery="inline",
                    ),
                ),
            )
        ),
        Diagnostics(stderr=io.StringIO()),
        stdout=io.StringIO(),
    )

    manager.emit({"sequence": 1})
    manager.emit({"sequence": 2})
    before = time.perf_counter()
    manager.emit({"sequence": 3})

    assert time.perf_counter() - before < 0.1
    assert close_started.wait(1)
    release.set()
    manager.close(1)


def test_inline_shutdown_cleanup_is_bounded_and_repeatable() -> None:
    close_started = threading.Event()
    release = threading.Event()
    diagnostics = Diagnostics(stderr=io.StringIO())
    close_calls = 0

    class BlockingCloseWriter(RecordingWriter):
        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1
            close_started.set()
            release.wait(1)

    manager = DeliveryManager(
        make_config(
            logging=LoggingConfig(
                destinations=(
                    CustomLogDestination(
                        name="blocking-inline",
                        writer_factory=BlockingCloseWriter,
                        delivery="inline",
                    ),
                ),
                shutdown_timeout_seconds=0.01,
            )
        ),
        diagnostics,
        stdout=io.StringIO(),
    )

    before = time.perf_counter()
    manager.close(0.01)
    manager.close(0.01)

    assert time.perf_counter() - before < 0.2
    assert close_started.is_set()
    assert close_calls == 1
    assert diagnostics.count("logs.shutdown_timeout") == 1
    release.set()


def test_inline_cleanup_failure_is_contained() -> None:
    close_attempted = threading.Event()
    diagnostics = Diagnostics(stderr=io.StringIO())

    class FailingCloseWriter(RecordingWriter):
        def close(self) -> None:
            close_attempted.set()
            raise RuntimeError("close failed")

    manager = DeliveryManager(
        make_config(
            logging=LoggingConfig(
                destinations=(
                    CustomLogDestination(
                        name="failing-close",
                        writer_factory=FailingCloseWriter,
                        delivery="inline",
                    ),
                ),
            )
        ),
        diagnostics,
        stdout=io.StringIO(),
    )

    manager.close(1)

    assert close_attempted.is_set()
    assert diagnostics.count("failure.logging.writer_close") == 1


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
            assert name == "application"
            return Logger()

        def close(self):
            self.closed = True

    monkeypatch.setattr(logging_v2, "Client", Client)
    monkeypatch.setattr(
        "policyengine_observability.google_credentials.load_google_credentials",
        lambda **_kwargs: "credentials",
    )
    writer = _GoogleCloudWriter(
        GoogleCloudLogDestination(
            project_id="central",
            log_name="application",
            write_timeout_seconds=2,
        )
    )
    writer.write_many(
        [
            {
                "severity": "WARNING",
                "trace_id": "a",
                "span_id": "b",
                "trace_sampled": True,
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
