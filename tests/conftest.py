from __future__ import annotations

import pytest
from fakes import RecordingDestination

from policyengine_observability import _state
from policyengine_observability.destinations.registry import (
    _STRATEGIES,
    register_destination,
)


@pytest.fixture(autouse=True)
def isolated_observability_context():
    """Keep tests independent when request and operation tests run in separate files."""
    variables = (
        (_state._REQUEST_CONTEXT, None),
        (_state._OPERATION_CONTEXT, None),
        (_state._TIMINGS, None),
        (_state._TURN_START, None),
        (_state._SEGMENT_STACK, ()),
    )
    for variable, default in variables:
        variable.set(default)
    try:
        yield
    finally:
        for variable, default in variables:
            variable.set(default)


@pytest.fixture
def fake_remote_strategy():
    """Register a fake remote strategy; yields its destination name."""
    register_destination(
        "fake-remote",
        lambda **kwargs: RecordingDestination(),
        transport="remote",
    )
    yield "fake_remote"
    _STRATEGIES.pop("fake_remote", None)
