from __future__ import annotations

from typing import Any

from ..runtime import ObservabilityRuntime

_RUNTIME_ATTRIBUTE = "_policyengine_observability_runtime"


def instrument_httpx(client: Any, runtime: ObservabilityRuntime) -> Any:
    """Add correlation headers to requests from one supplied HTTPX client."""

    existing = getattr(client, _RUNTIME_ATTRIBUTE, None)
    if existing is runtime:
        return client
    if existing is not None:
        runtime.diagnostics.report(
            "httpx.already_instrumented",
            "The HTTPX client is already associated with another runtime.",
        )
        return client

    try:
        import httpx

        if isinstance(client, httpx.AsyncClient):

            async def inject_async(request: httpx.Request) -> None:
                _inject(request, runtime)

            hook = inject_async
        elif isinstance(client, httpx.Client):

            def inject_sync(request: httpx.Request) -> None:
                _inject(request, runtime)

            hook = inject_sync
        else:
            raise TypeError("client must be httpx.Client or httpx.AsyncClient")

        client.event_hooks.setdefault("request", []).append(hook)
        setattr(client, _RUNTIME_ATTRIBUTE, runtime)
    except Exception as exc:
        runtime.diagnostics.report("httpx.instrument", exc)
    return client


def _inject(request: Any, runtime: ObservabilityRuntime) -> None:
    try:
        headers: dict[str, str] = {}
        runtime.inject_http_headers(headers)
        for key, value in headers.items():
            request.headers[key] = value
    except Exception as exc:
        runtime.diagnostics.report("httpx.request_inject", exc)
