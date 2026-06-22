from __future__ import annotations

import logging


class PlainMessageFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def configure_plain_logger(logger: logging.Logger, level: int) -> None:
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(PlainMessageFormatter())
        logger.addHandler(handler)
