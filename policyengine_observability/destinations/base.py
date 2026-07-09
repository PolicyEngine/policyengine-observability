from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping, Sequence
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


def normalize_name(name: str) -> str:
    """Canonical lookup key for registered names.

    Destination, formatter, and profile lookups all forgive case,
    surrounding whitespace, and hyphen/underscore variance the same way,
    so a spelling that works for one registry works for every registry.
    """
    return name.strip().lower().replace("-", "_")


def accepts_keyword(func: Callable[..., Any], name: str) -> bool:
    """Whether ``func`` can safely be called with keyword ``name``."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def safe_report(
    on_failure: Callable[..., None],
    operation: str,
    exc: BaseException,
    **fields: Any,
) -> None:
    """Report through the internal-error channel; never raises."""
    try:
        on_failure(operation, exc, **fields)
    except Exception:
        pass


def close_destination(
    destination: LogDestination,
    *,
    on_failure: Callable[..., None],
    deadline_seconds: float | None = None,
) -> None:
    """Close a destination if it supports closing; never raises.

    ``close`` is duck-typed with one calling convention everywhere: the
    deadline is passed only when the signature accepts it, so both
    ``close(self)`` and ``close(self, deadline_seconds=None)`` work under
    every close path (manager shutdown, reconfigure, queued drain).
    """
    close = getattr(destination, "close", None)
    if not callable(close):
        return
    try:
        if accepts_keyword(close, "deadline_seconds"):
            close(deadline_seconds=deadline_seconds)
        else:
            close()
    except Exception as exc:
        safe_report(
            on_failure,
            "logging.destination_close",
            exc,
            destination=getattr(destination, "name", None),
        )


def clamped(value: Any, *, low: float, high: float, default: float) -> float:
    """Coerce a config knob to a finite float within [low, high].

    Anything unparseable or non-finite falls back to the default, so a
    stray env value can never disable or unbound the mechanism it tunes.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(max(number, low), high)


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
