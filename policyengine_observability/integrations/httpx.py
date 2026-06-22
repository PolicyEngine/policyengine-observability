from __future__ import annotations

from ..runtime import ObservabilityRuntime
from ..runtime import observability_runtime


def instrument_httpx(runtime: ObservabilityRuntime | None = None) -> None:
    runtime = runtime or observability_runtime()
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except BaseException as exc:
        runtime.log_observability_failure("httpx.auto_instrument", exc)
