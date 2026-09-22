from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TextIO, runtime_checkable

DeliveryMode = Literal["inline", "queued"]
RecordFormatter = Callable[[dict[str, Any]], dict[str, Any]]


class RecordWriter(Protocol):
    """Writes provider-neutral structured records to one destination."""

    def write(self, record: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class DestinationBuildContext:
    """Process-local resources available while constructing a writer."""

    stdout: Callable[[], TextIO]


@runtime_checkable
class LogDestinationStrategy(Protocol):
    """Configuration and factory contract for a logging destination."""

    @property
    def name(self) -> str: ...

    @property
    def delivery(self) -> DeliveryMode: ...

    @property
    def queue_capacity(self) -> int: ...

    @property
    def batch_size(self) -> int: ...

    def build_writer(
        self, context: DestinationBuildContext
    ) -> RecordWriter: ...


class _FormattingWriter:
    def __init__(
        self,
        writer: RecordWriter,
        formatter: RecordFormatter | None,
    ) -> None:
        self._writer = writer
        self._formatter = formatter

    def write(self, record: dict[str, Any]) -> None:
        self._writer.write(self._format(record))

    def write_many(self, records: list[dict[str, Any]]) -> None:
        formatted = [self._format(record) for record in records]
        write_many = getattr(self._writer, "write_many", None)
        if callable(write_many):
            write_many(formatted)
            return
        for record in formatted:
            self._writer.write(record)

    def close(self) -> None:
        close = getattr(self._writer, "close", None)
        if callable(close):
            close()

    def _format(self, record: dict[str, Any]) -> dict[str, Any]:
        value = record.copy()
        return self._formatter(value) if self._formatter else value


@dataclass(frozen=True, slots=True)
class CustomLogDestination:
    """Adapts an application or third-party writer into the runtime.

    Queued delivery is the safe default. Use inline delivery only for writers
    that perform bounded local work and never make network requests.
    """

    name: str
    writer_factory: Callable[[], RecordWriter]
    delivery: DeliveryMode = "queued"
    queue_capacity: int = 1_000
    batch_size: int = 1
    formatter: RecordFormatter | None = None

    def diagnostics(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.name.strip():
            errors.append("Custom log destination name must be non-empty.")
        if not callable(self.writer_factory):
            errors.append(
                "Custom log destination writer_factory must be callable."
            )
        if self.formatter is not None and not callable(self.formatter):
            errors.append("Custom log destination formatter must be callable.")
        return tuple(errors)

    def build_writer(self, _context: DestinationBuildContext) -> RecordWriter:
        return _FormattingWriter(self.writer_factory(), self.formatter)
