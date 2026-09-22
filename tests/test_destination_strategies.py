from __future__ import annotations

import io
import json
import threading

from conftest import make_config

from policyengine_observability import (
    CustomLogDestination,
    DeploymentIdentity,
    GoogleCloudLogFormatter,
    LoggingConfig,
    StdoutLogDestination,
)
from policyengine_observability.delivery import DeliveryManager
from policyengine_observability.destinations import DestinationBuildContext
from policyengine_observability.diagnostics import Diagnostics


class RecordingWriter:
    def __init__(self) -> None:
        self.records: list[dict] = []
        self.written = threading.Event()

    def write(self, record: dict) -> None:
        self.records.append(record)
        self.written.set()

    def close(self) -> None:
        pass


class FailingWriter:
    def write(self, _record: dict) -> None:
        raise RuntimeError("destination failed")


def test_remote_destination_selection_is_independent_of_platform() -> None:
    writer = RecordingWriter()
    manager = DeliveryManager(
        make_config(
            deployment=DeploymentIdentity("test", "other"),
            logging=LoggingConfig(
                destinations=(
                    CustomLogDestination(
                        name="recording",
                        writer_factory=lambda: writer,
                        delivery="queued",
                    ),
                )
            ),
        ),
        Diagnostics(),
    )

    manager.emit({"event.name": "portable"})

    assert writer.written.wait(1)
    assert writer.records == [{"event.name": "portable"}]
    manager.close(1)


def test_multiple_runtimes_can_write_to_separate_destinations() -> None:
    first = RecordingWriter()
    second = RecordingWriter()

    def manager_for(writer: RecordingWriter) -> DeliveryManager:
        return DeliveryManager(
            make_config(
                logging=LoggingConfig(
                    destinations=(
                        CustomLogDestination(
                            name="isolated",
                            writer_factory=lambda: writer,
                            delivery="queued",
                        ),
                    )
                )
            ),
            Diagnostics(),
        )

    first_manager = manager_for(first)
    second_manager = manager_for(second)
    first_manager.emit({"consumer": "first-api"})
    second_manager.emit({"consumer": "household-api"})

    assert first.written.wait(1)
    assert second.written.wait(1)
    assert first.records == [{"consumer": "first-api"}]
    assert second.records == [{"consumer": "household-api"}]
    first_manager.close(1)
    second_manager.close(1)


def test_google_fields_are_added_by_destination_formatter() -> None:
    output = io.StringIO()
    manager = DeliveryManager(
        make_config(
            logging=LoggingConfig(
                destinations=(
                    StdoutLogDestination(
                        formatter=GoogleCloudLogFormatter("trace-project")
                    ),
                )
            )
        ),
        Diagnostics(),
        stdout=output,
    )

    manager.emit(
        {
            "severity": "INFO",
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "trace_sampled": True,
        }
    )

    record = json.loads(output.getvalue())
    assert record["logging.googleapis.com/trace"] == (
        "projects/trace-project/traces/" + "a" * 32
    )
    assert record["logging.googleapis.com/spanId"] == "b" * 16
    assert record["logging.googleapis.com/trace_sampled"] is True


def test_one_destination_failure_does_not_affect_another_destination() -> None:
    successful = RecordingWriter()
    diagnostics = Diagnostics()
    manager = DeliveryManager(
        make_config(
            logging=LoggingConfig(
                destinations=(
                    CustomLogDestination(
                        name="failing",
                        writer_factory=FailingWriter,
                        delivery="inline",
                    ),
                    CustomLogDestination(
                        name="successful",
                        writer_factory=lambda: successful,
                        delivery="inline",
                    ),
                )
            )
        ),
        diagnostics,
    )

    for sequence in range(4):
        manager.emit({"sequence": sequence})

    assert successful.records == [
        {"sequence": 0},
        {"sequence": 1},
        {"sequence": 2},
        {"sequence": 3},
    ]
    assert diagnostics.count("logs.export_failure") == 3
    assert diagnostics.count("failure.logging.destination_disabled") == 1


def test_formatters_receive_deeply_isolated_records() -> None:
    formatted = RecordingWriter()
    unchanged = RecordingWriter()
    source = {"attributes": {"backend": "original"}}

    def mutate_nested(record: dict) -> dict:
        record["attributes"]["backend"] = "formatted"
        return record

    manager = DeliveryManager(
        make_config(
            logging=LoggingConfig(
                destinations=(
                    CustomLogDestination(
                        name="formatted",
                        writer_factory=lambda: formatted,
                        delivery="inline",
                        formatter=mutate_nested,
                    ),
                    CustomLogDestination(
                        name="unchanged",
                        writer_factory=lambda: unchanged,
                        delivery="inline",
                    ),
                )
            )
        ),
        Diagnostics(),
    )

    manager.emit(source)

    assert formatted.records[0]["attributes"]["backend"] == "formatted"
    assert unchanged.records[0]["attributes"]["backend"] == "original"
    assert source["attributes"]["backend"] == "original"
    manager.close(1)


def test_destination_writers_copy_nested_values_before_formatting() -> None:
    source = {"attributes": {"backend": "original"}}

    def mutate_nested(record: dict) -> dict:
        record["attributes"]["backend"] = "formatted"
        return record

    output = io.StringIO()
    context = DestinationBuildContext(stdout=lambda: output)
    stdout_writer = StdoutLogDestination(formatter=mutate_nested).build_writer(
        context
    )
    stdout_writer.write(source)

    assert json.loads(output.getvalue())["attributes"]["backend"] == (
        "formatted"
    )
    assert source["attributes"]["backend"] == "original"

    recording = RecordingWriter()
    custom_writer = CustomLogDestination(
        name="custom",
        writer_factory=lambda: recording,
        delivery="inline",
        formatter=mutate_nested,
    ).build_writer(context)
    custom_writer.write(source)

    assert recording.records[0]["attributes"]["backend"] == "formatted"
    assert source["attributes"]["backend"] == "original"


def test_slow_remote_destination_does_not_delay_another_destination() -> None:
    started = threading.Event()
    release = threading.Event()
    successful = RecordingWriter()

    class BlockingWriter:
        def write(self, _record: dict) -> None:
            started.set()
            release.wait(1)

    manager = DeliveryManager(
        make_config(
            logging=LoggingConfig(
                destinations=(
                    CustomLogDestination(
                        name="blocking",
                        writer_factory=BlockingWriter,
                    ),
                    CustomLogDestination(
                        name="successful",
                        writer_factory=lambda: successful,
                    ),
                )
            )
        ),
        Diagnostics(),
    )

    manager.emit({"event.name": "independent"})

    assert started.wait(1)
    assert successful.written.wait(0.2)
    release.set()
    manager.close(1)
