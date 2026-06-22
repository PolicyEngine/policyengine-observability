from __future__ import annotations

from enum import Enum
from typing import Any, Iterable


UNKNOWN_SEGMENT = "unknown_segment"


def segment_values(
    registry: type[Enum] | Iterable[str] | None,
) -> frozenset[str]:
    if registry is None:
        return frozenset()
    if isinstance(registry, type) and issubclass(registry, Enum):
        return frozenset(str(member.value) for member in registry)
    return frozenset(str(value) for value in registry)


def coerce_segment_name(
    value: Any,
    *,
    registry: type[Enum] | Iterable[str] | None = None,
) -> tuple[str, bool]:
    values = segment_values(registry)
    if isinstance(value, Enum):
        segment = str(value.value)
        return segment, not values or segment in values
    if isinstance(value, str):
        return value, not values or value in values
    try:
        segment = str(value)
    except BaseException:
        return UNKNOWN_SEGMENT, False
    return segment, False if values else True
