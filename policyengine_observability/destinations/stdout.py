from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from .base import normalize_payload


class StdoutJsonDestination:
    name = "stdout"

    def __init__(
        self,
        *,
        loggers: Mapping[str, logging.Logger],
        serializer: Callable[[dict[str, Any]], str],
    ) -> None:
        self.loggers = loggers
        self.serializer = serializer

    def emit(
        self,
        payload: dict[str, Any],
        *,
        log_type: str,
        severity: str,
    ) -> None:
        logger = self.loggers.get(log_type) or self.loggers["event"]
        message = self.serializer(normalize_payload(payload))
        if severity in {"ERROR", "CRITICAL"}:
            logger.error(message)
        elif severity == "WARNING":
            logger.warning(message)
        else:
            logger.info(message)
