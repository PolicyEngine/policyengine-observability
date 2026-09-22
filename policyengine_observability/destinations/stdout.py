from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from .base import (
    DestinationBuildContext,
    RecordFormatter,
    RecordWriter,
)


class _StdoutWriter:
    def __init__(
        self,
        context: DestinationBuildContext,
        formatter: RecordFormatter | None,
    ) -> None:
        self._context = context
        self._formatter = formatter

    def write(self, record: dict[str, Any]) -> None:
        value = record.copy()
        if self._formatter is not None:
            value = self._formatter(value)
        print(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            file=self._context.stdout(),
            flush=True,
        )


@dataclass(frozen=True, slots=True)
class StdoutLogDestination:
    """Writes one JSON object per line to the process standard output."""

    formatter: RecordFormatter | None = None
    name: str = "stdout"
    delivery: Literal["inline"] = "inline"
    queue_capacity: int = 1
    batch_size: int = 1

    def build_writer(self, context: DestinationBuildContext) -> RecordWriter:
        return _StdoutWriter(context, self.formatter)
