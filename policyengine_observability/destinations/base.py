from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class LogDestination(Protocol):
    name: str

    def emit(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> None:
        """Write one structured observability payload."""


def normalize_payload(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): normalize_payload(item) for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [normalize_payload(item) for item in value]
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return repr(value)
    try:
        return str(value)
    except BaseException:
        return f"<unprintable {type(value).__name__}>"
