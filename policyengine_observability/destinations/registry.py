"""Named destination strategies.

Backend modules register themselves here so the manager can build
destinations without knowing any backend: an ``inline`` strategy writes
synchronously on the caller's thread (stdout — the durable, dependency-
free record), while every ``remote`` strategy is wrapped in the bounded
queue transport, so no remote write can ever run on a request thread.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .base import LogDestination

# Factories are called with keyword arguments (config, loggers,
# serializer) and may ignore what they do not need.
DestinationFactory = Callable[..., LogDestination]


@dataclass(frozen=True)
class DestinationStrategy:
    factory: DestinationFactory
    transport: Literal["inline", "remote"]


_STRATEGIES: dict[str, DestinationStrategy] = {}


def register_destination(
    name: str,
    factory: DestinationFactory,
    *,
    transport: Literal["inline", "remote"],
    aliases: tuple[str, ...] = (),
) -> None:
    strategy = DestinationStrategy(factory=factory, transport=transport)
    for key in (name, *aliases):
        _STRATEGIES[_normalize(key)] = strategy


def destination_strategy(name: str) -> DestinationStrategy | None:
    return _STRATEGIES.get(_normalize(name))


def _normalize(name: str) -> str:
    return name.strip().lower().replace("-", "_")
