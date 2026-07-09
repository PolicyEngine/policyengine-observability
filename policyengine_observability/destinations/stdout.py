from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from .base import normalize_name, normalize_payload, safe_report
from .registry import register_destination

# A stdout formatter shapes the normalized payload into the JSON line a
# platform's log agent expects. Formatters are registered by name so
# backend modules can contribute agent-native shapes without the core
# knowing about any backend; "plain" is the built-in default. Factories
# receive the ObservabilityConfig so a formatter can close over settings
# it needs (duck-typed to keep this module config-agnostic).
StdoutFormatter = Callable[..., dict[str, Any]]
StdoutFormatterFactory = Callable[[Any], StdoutFormatter]

_FORMATTER_FACTORIES: dict[str, StdoutFormatterFactory] = {}


def register_stdout_formatter(
    name: str, factory: StdoutFormatterFactory
) -> None:
    _FORMATTER_FACTORIES[normalize_name(name)] = factory


def resolve_stdout_formatter(
    config: Any,
    on_failure: Callable[..., None] | None = None,
) -> StdoutFormatter:
    raw = getattr(config, "stdout_format", None) or "plain"
    factory = _FORMATTER_FACTORIES.get(normalize_name(raw))
    if factory is None:
        # Falling back must not lose the record, but a typo'd format
        # name should not pass silently either — the agent-native shape
        # it named would just quietly never appear.
        if on_failure is not None:
            safe_report(
                on_failure,
                "logging.stdout_format",
                ValueError(f"Unknown stdout format {raw!r}; using plain."),
            )
        factory = _FORMATTER_FACTORIES["plain"]
    return factory(config)


def _plain_formatter_factory(config: Any) -> StdoutFormatter:
    def format_plain(
        payload: dict[str, Any], *, log_type: str, severity: str
    ) -> dict[str, Any]:
        return payload

    return format_plain


register_stdout_formatter("plain", _plain_formatter_factory)


class StdoutJsonDestination:
    name = "stdout"

    def __init__(
        self,
        *,
        loggers: Mapping[str, logging.Logger],
        serializer: Callable[[dict[str, Any]], str],
        formatter: StdoutFormatter | None = None,
    ) -> None:
        self.loggers = loggers
        self.serializer = serializer
        self.formatter = formatter or _plain_formatter_factory(None)

    def emit(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> None:
        normalized = normalize_payload(payload)
        try:
            # Formatters receive (and may mutate) the private normalized
            # copy. Stdout is the fallback sink, so a broken formatter
            # degrades to the unformatted line rather than losing the
            # record or tripping the breaker.
            formatted = self.formatter(
                normalized, log_type=log_type, severity=severity
            )
        except Exception:
            formatted = normalized
        message = self.serializer(formatted)
        logger = self.loggers.get(log_type) or self.loggers["event"]
        if severity in {"ERROR", "CRITICAL"}:
            logger.error(message)
        elif severity == "WARNING":
            logger.warning(message)
        else:
            logger.info(message)


def build_stdout_destination(
    *,
    config: Any,
    loggers: Any,
    serializer: Any,
    on_failure: Callable[..., None] | None = None,
    **_: Any,
) -> StdoutJsonDestination:
    """The one place a configured stdout destination is assembled.

    Used both as the registered ``stdout`` strategy factory and by the
    manager's fail-open fallback, so formatter resolution can never
    diverge between the two paths.
    """
    return StdoutJsonDestination(
        loggers=loggers,
        serializer=serializer,
        formatter=resolve_stdout_formatter(config, on_failure=on_failure),
    )


register_destination("stdout", build_stdout_destination, transport="inline")
