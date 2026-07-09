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

from .base import LogDestination, normalize_name

# Factories are called with keyword arguments (config, loggers,
# serializer, on_failure) and may ignore what they do not need. The
# on_failure callable is for construction-time reporting only; reports
# made through it may be deferred until the build completes.
DestinationFactory = Callable[..., LogDestination]


@dataclass(frozen=True)
class DestinationStrategy:
    factory: DestinationFactory
    transport: Literal["inline", "remote"]
    # Config attribute names that must resolve truthy for the strategy
    # to be usable. Profiles naming this strategy downgrade gracefully
    # (with a warning) when a requirement is missing, instead of failing
    # at construction and falling back with a config-failure report.
    required_config: tuple[str, ...] = ()


_STRATEGIES: dict[str, DestinationStrategy] = {}


def register_destination(
    name: str,
    factory: DestinationFactory,
    *,
    transport: Literal["inline", "remote"],
    aliases: tuple[str, ...] = (),
    required_config: tuple[str, ...] = (),
) -> None:
    strategy = DestinationStrategy(
        factory=factory,
        transport=transport,
        required_config=required_config,
    )
    for key in (name, *aliases):
        _STRATEGIES[normalize_name(key)] = strategy


def destination_strategy(name: str) -> DestinationStrategy | None:
    return _STRATEGIES.get(normalize_name(name))
