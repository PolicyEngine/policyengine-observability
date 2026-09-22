from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pytest

from policyengine_observability import (
    DeploymentIdentity,
    LoggingConfig,
    ObservabilityConfig,
    OTelConfig,
    ServiceIdentity,
    configure,
)
from policyengine_observability.runtime import ObservabilityRuntime


def make_config(**overrides):
    values = {
        "service": ServiceIdentity(
            name="test-api",
            namespace="policyengine.test",
            version="2.3.4",
            role="entry",
        ),
        "deployment": DeploymentIdentity(
            environment="test",
            platform="local",
            region="us-central1",
            instance_id="instance-1",
        ),
        "logging": LoggingConfig(),
        "otel": OTelConfig(enabled=False),
        "application_attribute_keys": frozenset({"auth_result", "backend"}),
        "dispatch_attribute_keys": frozenset(
            {"job_id", "run_id", "simulation_id"}
        ),
    }
    values.update(overrides)
    return ObservabilityConfig(**values)


def make_runtime(**overrides) -> tuple[ObservabilityRuntime, io.StringIO]:
    output = io.StringIO()
    runtime = configure(make_config(**overrides))
    runtime._delivery._stdout = output
    return runtime, output


def records(output: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


@pytest.fixture
def runtime() -> Iterator[tuple[ObservabilityRuntime, io.StringIO]]:
    value = make_runtime()
    yield value
    value[0].shutdown()
