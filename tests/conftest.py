from __future__ import annotations

import pytest
from fakes import RecordingDestination

from policyengine_observability.destinations.registry import (
    _STRATEGIES,
    register_destination,
)


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
