from __future__ import annotations

import asyncio
import logging
import os
import select
import signal
import threading
import time

import pytest
from conftest import make_config, make_runtime, records

from policyengine_observability import (
    REQUEST_ID_HEADER,
    ConfigurationError,
    LoggingConfig,
    OTelConfig,
    configure,
    instrument_logging,
)


def test_operation_context_emits_one_completion(runtime) -> None:
    observed, output = runtime
    with observed.operation("simulation.run", attributes={"backend": "modal"}):
        observed.event("simulation.started")

    emitted = records(output)
    assert [item["event.name"] for item in emitted] == [
        "simulation.started",
        "operation.completed",
    ]
    completion = emitted[-1]
    assert completion["operation.name"] == "simulation.run"
    assert completion["outcome"] == "success"
    assert completion["attributes"] == {"backend": "modal"}


def test_span_does_not_emit_operation_completion(runtime) -> None:
    observed, output = runtime
    with observed.span("simulation.build"):
        observed.event("inside")
    assert [item["event.name"] for item in records(output)] == ["inside"]


def test_sync_decorator_preserves_result(runtime) -> None:
    observed, output = runtime

    @observed.operation("calculate")
    def calculate(value: int) -> int:
        return value * 2

    assert calculate(4) == 8
    assert records(output)[0]["event.name"] == "operation.completed"


def test_async_context_and_decorator_preserve_result(runtime) -> None:
    observed, output = runtime

    @observed.span("child")
    async def child() -> str:
        await asyncio.sleep(0)
        return "done"

    async def run() -> str:
        async with observed.operation("async.run"):
            return await child()

    assert asyncio.run(run()) == "done"
    assert [item["event.name"] for item in records(output)] == [
        "operation.completed"
    ]


def test_application_exception_is_same_object_when_export_fails(
    runtime,
) -> None:
    observed, _output = runtime
    application_error = RuntimeError("application failure")

    def broken_emit(_record) -> None:
        raise OSError("logging unavailable")

    observed._delivery.emit = broken_emit

    @observed.operation("broken")
    def fail() -> None:
        raise application_error

    with pytest.raises(RuntimeError) as caught:
        fail()
    assert caught.value is application_error
    assert observed.diagnostics.count("failure.record.emit") == 1


def test_observability_entry_failure_does_not_change_return(
    monkeypatch,
) -> None:
    observed, _output = make_runtime()

    def fail_start(*_args, **_kwargs):
        raise RuntimeError("instrumentation failed")

    monkeypatch.setattr(observed, "_start_operation", fail_start)

    @observed.operation("work")
    def work() -> int:
        return 42

    assert work() == 42
    assert observed.diagnostics.count("failure.operation.start") == 1
    observed.shutdown()


def test_process_control_exception_from_instrumentation_is_not_caught(
    monkeypatch,
) -> None:
    observed, _output = make_runtime()

    def stop(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(observed, "_start_operation", stop)
    with pytest.raises(KeyboardInterrupt):
        with observed.operation("work"):
            pass
    observed.shutdown()


def test_request_accepts_valid_id_and_emits_once(runtime) -> None:
    observed, output = runtime
    request_id = observed.begin_request(
        headers={REQUEST_ID_HEADER.lower(): "request-123"},
        method="get",
        route="/household/<id>",
    )
    assert request_id == "request-123"
    assert observed.response_headers()[REQUEST_ID_HEADER] == "request-123"
    observed.end_request(status_code=200)
    observed.end_request(status_code=200)
    completion = records(output)[0]
    assert completion["event.name"] == "request.completed"
    assert completion["request.id"] == "request-123"
    assert completion["http.request.method"] == "GET"
    assert completion["http.route"] == "/household/<id>"
    assert completion["http.response.status_code"] == 200


def test_request_replaces_malformed_id_and_classifies_status(runtime) -> None:
    observed, output = runtime
    request_id = observed.begin_request(
        headers={REQUEST_ID_HEADER: "invalid id with spaces"},
        method="POST",
        route="<unmatched>",
    )
    assert request_id != "invalid id with spaces"
    observed.update_request_route("/economy")
    observed.end_request(status_code=404)
    assert records(output)[0]["outcome"] == "client_error"


def test_request_exception_is_error(runtime) -> None:
    observed, output = runtime
    observed.begin_request(headers={}, method="GET", route="/fail")
    error = ValueError("bad")
    observed.record_exception(error, handled=False, status_code=500)
    observed.end_request(status_code=500, error=error)
    emitted = records(output)
    assert emitted[0]["event.name"] == "exception.recorded"
    assert emitted[1]["event.name"] == "request.completed"
    assert emitted[1]["outcome"] == "error"


def test_set_context_and_capture_context_are_allowlisted(runtime) -> None:
    observed, _output = runtime
    observed.begin_request(
        headers={REQUEST_ID_HEADER: "request-1"},
        method="POST",
        route="/simulation",
    )
    observed.set_context(
        auth_result="accepted",
        job_id="job-1",
        household="must-not-appear",
        unapproved="must-not-appear",
    )
    captured = observed.capture_context()
    assert captured["request_id"] == "request-1"
    assert captured["job_id"] == "job-1"
    assert "auth_result" not in captured
    assert "household" not in captured
    assert "unapproved" not in captured
    assert "captured_at" in captured
    assert observed.diagnostics.count("attributes.omitted") == 2
    observed.end_request(status_code=202)


def test_two_runtimes_keep_identity_and_context_separate() -> None:
    first, first_output = make_runtime()
    second_config = make_config(
        service=make_config().service.__class__(
            "second-api", "policyengine.test", "1", "worker"
        )
    )
    second = configure(second_config)
    import io

    second_output = io.StringIO()
    second._delivery._stdout = second_output
    first.begin_request(
        headers={REQUEST_ID_HEADER: "first-request"},
        method="GET",
        route="/first",
    )
    second.event("second.event")
    first.end_request(status_code=200)
    assert records(first_output)[0]["request.id"] == "first-request"
    second_record = records(second_output)[0]
    assert second_record["service.name"] == "second-api"
    assert "request.id" not in second_record
    first.shutdown()
    second.shutdown()


def test_concurrent_async_tasks_do_not_share_request_ids() -> None:
    observed, output = make_runtime()

    async def request(request_id: str) -> None:
        observed.begin_request(
            headers={REQUEST_ID_HEADER: request_id},
            method="GET",
            route="/task",
        )
        await asyncio.sleep(0)
        observed.event("task.event")
        observed.end_request(status_code=200)

    async def run() -> None:
        await asyncio.gather(request("one"), request("two"))

    asyncio.run(run())
    grouped = {
        item["request.id"] for item in records(output) if "request.id" in item
    }
    assert grouped == {"one", "two"}
    observed.shutdown()


def test_standard_logging_is_explicit_and_idempotent(runtime) -> None:
    observed, output = runtime
    logger = logging.getLogger("tests.application")
    logger.handlers.clear()
    logger.propagate = False
    first = instrument_logging(logger, observed)
    second = instrument_logging(logger, observed)
    assert first is second
    logger.warning(
        "structured warning",
        extra={"policyengine_attributes": {"backend": "local"}},
    )
    item = records(output)[0]
    assert item["message"] == "structured warning"
    assert item["severity"] == "WARNING"
    assert item["attributes"] == {"backend": "local"}


def test_shutdown_is_bounded_repeatable_and_restartable(runtime) -> None:
    observed, _output = runtime
    observed.shutdown()
    observed.shutdown()
    assert observed._closed
    observed.restart_after_snapshot()
    assert not observed._closed


def test_shutdown_contains_delivery_failure_and_continues_cleanup(
    monkeypatch,
) -> None:
    observed, _output = make_runtime(
        logging=LoggingConfig(shutdown_timeout_seconds=0.125),
        otel=OTelConfig(enabled=False, shutdown_timeout_seconds=0.375),
    )
    otel_timeouts: list[float] = []

    class OTel:
        def shutdown(self, timeout: float) -> None:
            otel_timeouts.append(timeout)

    def fail_close(timeout: float) -> None:
        assert timeout == 0.125
        raise RuntimeError("logging close failed")

    logger = logging.getLogger("tests.shutdown.cleanup")
    logger.handlers.clear()
    logger.propagate = False
    handler = instrument_logging(logger, observed)
    observed._otel = OTel()
    monkeypatch.setattr(observed._delivery, "close", fail_close)

    observed.shutdown()
    observed.shutdown()

    assert otel_timeouts == [0.375]
    assert handler not in logger.handlers
    assert observed.diagnostics.count("failure.logging.shutdown") == 1


def test_shutdown_bounds_otel_with_its_own_timeout() -> None:
    observed, _output = make_runtime(
        logging=LoggingConfig(shutdown_timeout_seconds=0),
        otel=OTelConfig(enabled=False, shutdown_timeout_seconds=0.01),
    )
    started = threading.Event()
    release = threading.Event()

    class BlockingOTel:
        def shutdown(self, timeout: float) -> None:
            assert timeout == 0.01
            started.set()
            release.wait(1)

    observed._otel = BlockingOTel()
    before = time.perf_counter()
    observed.shutdown()
    elapsed = time.perf_counter() - before

    assert started.is_set()
    assert elapsed < 0.2
    assert observed.diagnostics.count("shutdown.timeout") == 1
    release.set()


def test_shutdown_contains_otel_coordinator_failure(monkeypatch) -> None:
    observed, _output = make_runtime()

    def fail_bounded_shutdown(*_args, **_kwargs) -> None:
        raise RuntimeError("could not start shutdown worker")

    monkeypatch.setattr(
        "policyengine_observability.runtime._run_bounded",
        fail_bounded_shutdown,
    )

    observed.shutdown()

    assert observed.diagnostics.count("failure.otel.shutdown") == 1


@pytest.mark.parametrize(
    ("logging_timeout", "otel_timeout"),
    [
        ("invalid", float("inf")),
        (-1.0, 100.0),
    ],
)
def test_invalid_shutdown_timeout_configuration_is_rejected(
    logging_timeout,
    otel_timeout,
) -> None:
    with pytest.raises(ConfigurationError) as raised:
        make_runtime(
            logging=LoggingConfig(shutdown_timeout_seconds=logging_timeout),
            otel=OTelConfig(
                enabled=False,
                shutdown_timeout_seconds=otel_timeout,
            ),
        )

    message = str(raised.value)
    assert "logging.shutdown_timeout_seconds" in message
    assert "otel.shutdown_timeout_seconds" in message


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_restart_after_snapshot_replaces_inherited_locked_state() -> None:
    observed, _output = make_runtime()
    observed._shutdown_lock.acquire()
    observed.diagnostics._lock.acquire()
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()

    if child_pid == 0:
        os.close(read_fd)
        try:
            observed.restart_after_snapshot()
            observed.diagnostics.increment("post_fork")
            observed.shutdown()
            os.write(write_fd, b"ok")
        except BaseException as error:
            os.write(write_fd, repr(error).encode()[:1_024])
        finally:
            os.close(write_fd)
            os._exit(0)

    os.close(write_fd)
    result = b""
    try:
        readable, _, _ = select.select([read_fd], [], [], 2)
        if not readable:
            os.kill(child_pid, signal.SIGKILL)
            pytest.fail("child blocked on inherited observability state")
        result = os.read(read_fd, 1_024)
    finally:
        observed.diagnostics._lock.release()
        observed._shutdown_lock.release()
        os.close(read_fd)
        os.waitpid(child_pid, 0)
        observed.shutdown()

    assert result == b"ok"


def test_restart_after_snapshot_rebuilds_process_local_components() -> None:
    observed, _output = make_runtime()
    observed.diagnostics.increment("before_snapshot")
    observed.begin_request(
        headers={REQUEST_ID_HEADER: "snapshotted-request"},
        method="GET",
        route="/snapshot",
    )
    inherited_delivery = observed._delivery
    inherited_otel = observed._otel

    observed.restart_after_snapshot()

    assert observed._delivery is not inherited_delivery
    assert observed._otel is not inherited_otel
    assert observed.response_headers() == {}
    assert observed.diagnostics.count("before_snapshot") == 0
    assert not observed._closed
    observed.shutdown()


def test_local_drop_and_export_diagnostics_increment_otel_metrics() -> None:
    observed, _output = make_runtime()
    calls: list[tuple[str, str, int]] = []

    class Metrics:
        def record_dropped(self, name: str, value: int) -> None:
            calls.append(("dropped", name, value))

        def record_exporter_failure(self, name: str, value: int) -> None:
            calls.append(("failure", name, value))

        def shutdown(self, _timeout: float) -> None:
            pass

    observed._otel = Metrics()
    observed.diagnostics.increment("logs.dropped.queue_full", 2)
    observed.diagnostics.increment("logs.export_failure", 1)
    observed.diagnostics.increment("unrelated", 1)
    assert calls == [
        ("dropped", "logs.dropped.queue_full", 2),
        ("failure", "logs.export_failure", 1),
    ]
    observed.shutdown()
