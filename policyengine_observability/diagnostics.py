from __future__ import annotations

import json
import sys
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any


class Diagnostics:
    """Rate-limited local reporting that never enters remote delivery."""

    def __init__(
        self,
        *,
        interval_seconds: float = 60.0,
        stderr: Any = None,
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        self._interval_seconds = max(1.0, interval_seconds)
        self._stderr = stderr or sys.stderr
        self._sensitive_values = sensitive_values
        self._last_report: dict[str, float] = {}
        self._counts: Counter[str] = Counter()
        self._listeners: list[Callable[[str, int], None]] = []
        self._lock = threading.Lock()

    def increment(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counts[name] += value
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(name, value)
            except Exception:
                continue

    def add_listener(self, listener: Callable[[str, int], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def count(self, name: str) -> int:
        with self._lock:
            return self._counts[name]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def restart_after_process_duplication(self) -> None:
        """Replace synchronization state inherited across process copying."""

        self._lock = threading.Lock()
        self._last_report = {}
        self._counts = Counter()

    def report(
        self,
        operation: str,
        error: Exception | str,
        **fields: Any,
    ) -> None:
        self.increment(f"failure.{operation}")
        now = monotonic()
        with self._lock:
            previous = self._last_report.get(operation)
            if (
                previous is not None
                and now - previous < self._interval_seconds
            ):
                return
            self._last_report[operation] = now

        record = {
            "schema_version": "policyengine.observability.internal.v1",
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "severity": "ERROR",
            "event.name": "observability.internal_failure",
            "operation": operation,
            "error.type": type(error).__name__,
            "error.message": _safe_text(error, self._sensitive_values),
            **{
                str(key): _safe_scalar(value, self._sensitive_values)
                for key, value in fields.items()
            },
        }
        try:
            print(
                json.dumps(record, sort_keys=True, ensure_ascii=True),
                file=self._stderr,
                flush=True,
            )
        except Exception:
            return


def _safe_text(value: Any, sensitive_values: tuple[str, ...]) -> str:
    try:
        text = str(value)
    except Exception:
        return "<unprintable>"
    for sensitive in sensitive_values:
        if sensitive:
            text = text.replace(sensitive, "[REDACTED]")
    return text[:2_048]


def _safe_scalar(
    value: Any, sensitive_values: tuple[str, ...]
) -> str | int | float | bool | None:
    if isinstance(value, str):
        return _safe_text(value, sensitive_values)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _safe_text(value, sensitive_values)
