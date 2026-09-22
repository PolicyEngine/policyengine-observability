from __future__ import annotations

import ast
import io
import json
import logging
import sys

from conftest import make_config, make_runtime, records

import policyengine_observability as observability
from policyengine_observability.diagnostics import Diagnostics

REMOVED_NAMES = {
    "segment",
    "asegment",
    "entrypoint",
    "set_attribute",
    "record_event",
    "record_error",
    "collect_timings",
    "start_scope",
    "annotate",
    "end_scope",
    "set_observability_runtime",
    "register_destination",
    "GoogleCloudLoggingConfig",
}


def test_public_api_contains_only_version_two_surface() -> None:
    assert REMOVED_NAMES.isdisjoint(observability.__all__)
    assert all(not hasattr(observability, name) for name in REMOVED_NAMES)
    assert (
        observability.configure(make_config()).config.service.name
        == "test-api"
    )


def test_import_does_not_require_framework_google_or_otel_sdk_modules() -> (
    None
):
    package_source = open(observability.__file__).read()
    tree = ast.parse(package_source)
    top_level_modules = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert top_level_modules.isdisjoint(
        {"google", "opentelemetry", "flask", "fastapi", "httpx"}
    )


def test_diagnostics_are_local_rate_limited_and_counted() -> None:
    output = io.StringIO()
    diagnostics = Diagnostics(stderr=output, interval_seconds=60)
    diagnostics.report("export.failed", ValueError("one"), attempt=1)
    diagnostics.report("export.failed", ValueError("two"), attempt=2)
    diagnostics.increment("dropped", 3)
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    item = json.loads(lines[0])
    assert item["operation"] == "export.failed"
    assert item["attempt"] == 1
    assert diagnostics.count("failure.export.failed") == 2
    assert diagnostics.count("dropped") == 3


def test_diagnostic_listener_failure_does_not_escape() -> None:
    diagnostics = Diagnostics(stderr=io.StringIO())
    diagnostics.add_listener(
        lambda _name, _value: (_ for _ in ()).throw(RuntimeError("listener"))
    )
    diagnostics.increment("still-counted", 2)
    assert diagnostics.count("still-counted") == 2


def test_standard_logging_ignores_observability_dependencies() -> None:
    runtime, output = make_runtime()
    logger = logging.getLogger("opentelemetry.exporter")
    logger.handlers.clear()
    logger.propagate = False
    observability.instrument_logging(logger, runtime)
    logger.error("would recurse")
    assert records(output) == []
    runtime.shutdown()


def test_automatic_standard_logging_installation(monkeypatch) -> None:
    logger = logging.getLogger()
    original_handlers = list(logger.handlers)
    try:
        config = make_config(
            logging=observability.LoggingConfig(
                capture_standard_library=True,
                replace_existing_handlers=True,
            )
        )
        runtime = observability.configure(config)
        runtime._delivery._stdout = io.StringIO()
        logging.getLogger("application.auto").warning("automatic")
        assert records(runtime._delivery._stdout)[0]["message"] == "automatic"
        runtime.shutdown()
    finally:
        logger.handlers[:] = original_handlers


def test_replace_logging_handlers_and_shutdown_removes_owned_handler() -> None:
    runtime, _ = make_runtime()
    logger = logging.getLogger("tests.replace")
    logger.handlers.clear()
    logger.addHandler(logging.StreamHandler(sys.stderr))
    handler = observability.instrument_logging(logger, runtime, replace=True)
    assert logger.handlers == [handler]
    runtime.shutdown()
    assert logger.handlers == []
