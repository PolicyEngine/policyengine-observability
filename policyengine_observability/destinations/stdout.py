from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from .base import normalize_payload
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
    _FORMATTER_FACTORIES[name.strip().lower()] = factory


def resolve_stdout_formatter(config: Any) -> StdoutFormatter:
    name = (getattr(config, "stdout_format", None) or "plain").strip().lower()
    factory = _FORMATTER_FACTORIES.get(name) or _FORMATTER_FACTORIES["plain"]
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


def _stdout_factory(*, config: Any, loggers: Any, serializer: Any, **_: Any):
    return StdoutJsonDestination(
        loggers=loggers,
        serializer=serializer,
        formatter=resolve_stdout_formatter(config),
    )


register_destination("stdout", _stdout_factory, transport="inline")
