from __future__ import annotations

from typing import Any

from ..runtime import ObservabilityRuntime

_EXTENSION_KEY = "policyengine_observability"


def instrument_flask(
    app: Any, runtime: ObservabilityRuntime
) -> ObservabilityRuntime:
    """Install one request lifecycle integration on a Flask application."""

    existing = app.extensions.get(_EXTENSION_KEY)
    if isinstance(existing, ObservabilityRuntime):
        return existing

    app.extensions[_EXTENSION_KEY] = runtime

    @app.before_request
    def _begin_observed_request() -> None:
        try:
            from flask import request

            route = (
                request.url_rule.rule if request.url_rule else "<unmatched>"
            )
            runtime.begin_request(
                headers=dict(request.headers),
                method=request.method,
                route=route,
            )
        except Exception as exc:
            runtime.diagnostics.report("flask.request_begin", exc)

    @app.after_request
    def _finish_observed_request(response: Any) -> Any:
        try:
            from flask import request

            if request.url_rule is not None:
                runtime.update_request_route(request.url_rule.rule)
            for key, value in runtime.response_headers().items():
                response.headers[key] = value
            runtime.end_request(status_code=response.status_code)
        except Exception as exc:
            runtime.diagnostics.report("flask.request_finish", exc)
        return response

    @app.teardown_request
    def _close_observed_request(error: BaseException | None) -> None:
        try:
            runtime.end_request(
                status_code=500 if error is not None else None,
                error=error,
            )
        except Exception as exc:
            runtime.diagnostics.report("flask.request_teardown", exc)

    return runtime
