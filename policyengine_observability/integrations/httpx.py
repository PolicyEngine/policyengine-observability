from __future__ import annotations

from ..runtime import ObservabilityRuntime
from ..runtime import observability_runtime


def instrument_httpx(runtime: ObservabilityRuntime | None = None) -> None:
    runtime = runtime or observability_runtime()
    runtime.instrument_httpx()
