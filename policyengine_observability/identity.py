"""Stable identities for individual telemetry-producing processes."""

from __future__ import annotations

import os
from uuid import uuid4

_PROCESS_IDENTITIES: dict[tuple[int, str, str], str] = {}


def process_instance_id(
    service_name: str,
    platform_instance_id: str | None = None,
) -> str:
    """Return a stable, unique identity for one service process.

    Platform identifiers such as a Cloud Run revision identify deployed code,
    not an individual process. The process ID distinguishes local workers and
    the UUID distinguishes processes in separate containers that reuse the
    same operating-system process ID. Including ``os.getpid()`` in the cache
    key causes a forked child to receive a new identity on its first call.
    """

    service = service_name.strip()
    if not service:
        raise ValueError("service_name must be non-empty")
    platform_id = (platform_instance_id or "unassigned").strip()
    if not platform_id:
        platform_id = "unassigned"
    process_id = os.getpid()
    key = (process_id, service, platform_id)
    existing = _PROCESS_IDENTITIES.get(key)
    if existing is not None:
        return existing
    generated = f"{platform_id}:{process_id}:{uuid4().hex}"
    return _PROCESS_IDENTITIES.setdefault(key, generated)
