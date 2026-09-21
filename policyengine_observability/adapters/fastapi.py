from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..runtime import ObservabilityRuntime

_STATE_KEY = "policyengine_observability"


def instrument_fastapi(
    app: Any, runtime: ObservabilityRuntime
) -> ObservabilityRuntime:
    """Install one request lifecycle integration on a FastAPI application."""

    existing = getattr(app.state, _STATE_KEY, None)
    if isinstance(existing, ObservabilityRuntime):
        return existing
    try:
        app.add_middleware(_ObservabilityMiddleware, runtime=runtime)
        setattr(app.state, _STATE_KEY, runtime)
    except Exception as exc:
        runtime.diagnostics.report("fastapi.middleware_install", exc)
    return runtime


class _ObservabilityMiddleware:
    def __init__(self, app: Any, *, runtime: ObservabilityRuntime) -> None:
        self.app = app
        self.runtime = runtime

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        status_code: int | None = None
        error: Exception | None = None
        completed = False
        try:
            self.runtime.begin_request(
                headers=_headers_from_scope(scope),
                method=str(scope.get("method") or ""),
                route=_route_from_scope(scope) or "<unmatched>",
            )
        except Exception as exc:
            self.runtime.diagnostics.report("fastapi.request_begin", exc)

        async def send_observed(message: dict[str, Any]) -> None:
            nonlocal completed, status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status") or 0)
                self._update_route(scope)
                message = {
                    **message,
                    "headers": _merge_headers(
                        list(message.get("headers") or []),
                        self.runtime.response_headers(),
                    ),
                }
            await send(message)
            if message.get("type") == "http.response.body" and not message.get(
                "more_body", False
            ):
                self._update_route(scope)
                self._end(status_code=status_code)
                completed = True

        try:
            await self.app(scope, receive, send_observed)
        except Exception as exc:
            error = exc
            raise
        finally:
            if not completed:
                self._update_route(scope)
                self._end(
                    status_code=status_code or (500 if error else None),
                    error=error,
                )

    def _update_route(self, scope: dict[str, Any]) -> None:
        try:
            route = _route_from_scope(scope)
            if route:
                self.runtime.update_request_route(route)
        except Exception as exc:
            self.runtime.diagnostics.report("fastapi.route_update", exc)

    def _end(
        self,
        *,
        status_code: int | None,
        error: BaseException | None = None,
    ) -> None:
        try:
            self.runtime.end_request(status_code=status_code, error=error)
        except Exception as exc:
            self.runtime.diagnostics.report("fastapi.request_finish", exc)


def _headers_from_scope(scope: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_key, raw_value in scope.get("headers") or []:
        try:
            key = raw_key.decode("latin-1").lower()
            value = raw_value.decode("latin-1")
        except (AttributeError, UnicodeDecodeError):
            continue
        headers[key] = f"{headers[key]},{value}" if key in headers else value
    return headers


def _route_from_scope(scope: dict[str, Any]) -> str | None:
    path = getattr(scope.get("route"), "path", None)
    return str(path) if path else None


def _merge_headers(
    existing: list[tuple[bytes, bytes]], values: dict[str, str]
) -> list[tuple[bytes, bytes]]:
    replacements = {key.lower().encode("latin-1") for key in values}
    merged = [
        (key, value)
        for key, value in existing
        if key.lower() not in replacements
    ]
    merged.extend(
        (key.encode("latin-1"), value.encode("latin-1"))
        for key, value in values.items()
    )
    return merged
