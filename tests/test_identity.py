from __future__ import annotations

import re

import pytest

from policyengine_observability import identity, process_instance_id


def test_process_instance_id_is_stable_within_one_process() -> None:
    first = process_instance_id("example-api", "revision-1")
    second = process_instance_id("example-api", "revision-1")

    assert first == second
    assert re.fullmatch(r"revision-1:\d+:[0-9a-f]{32}", first)


def test_process_instance_id_separates_services_and_deployments() -> None:
    first = process_instance_id("example-api", "revision-1")

    assert process_instance_id("other-api", "revision-1") != first
    assert process_instance_id("example-api", "revision-2") != first


def test_process_instance_id_changes_after_process_duplication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identity.os, "getpid", lambda: 10_001)
    parent = process_instance_id("example-api", "revision-1")
    monkeypatch.setattr(identity.os, "getpid", lambda: 10_002)
    child = process_instance_id("example-api", "revision-1")

    assert parent != child


def test_process_instance_id_rejects_empty_service_name() -> None:
    with pytest.raises(ValueError, match="service_name must be non-empty"):
        process_instance_id("  ")
