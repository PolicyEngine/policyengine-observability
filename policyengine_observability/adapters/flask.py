from __future__ import annotations

from typing import Any

from ..runtime import ObservabilityRuntime

_EXTENSION_KEY = "policyengine_observability"
_CALLBACK_REGISTRIES = (
    "before_request_funcs",
    "after_request_funcs",
    "teardown_request_funcs",
)
_MISSING = object()


def instrument_flask(
    app: Any, runtime: ObservabilityRuntime
) -> ObservabilityRuntime:
    """Install one request lifecycle integration on a Flask application."""

    try:
        extensions = app.extensions
        existing = extensions.get(_EXTENSION_KEY, _MISSING)
    except Exception as exc:
        runtime.diagnostics.report("flask.callback_install", exc)
        return runtime
    if isinstance(existing, ObservabilityRuntime):
        return existing

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

    def _finish_observed_request(response: Any) -> Any:
        try:
            from flask import request

            if request.url_rule is not None:
                runtime.update_request_route(request.url_rule.rule)
        except Exception as exc:
            runtime.diagnostics.report("flask.request_route", exc)
        try:
            runtime.update_request_status(response.status_code)
        except Exception as exc:
            runtime.diagnostics.report("flask.request_status", exc)
        try:
            for key, value in runtime.response_headers().items():
                response.headers[key] = value
        except Exception as exc:
            runtime.diagnostics.report("flask.response_headers", exc)
        return response

    def _close_observed_request(error: BaseException | None) -> None:
        try:
            runtime.end_request(
                status_code=500 if error is not None else None,
                error=error,
            )
        except Exception as exc:
            runtime.diagnostics.report("flask.request_teardown", exc)

    snapshots: dict[str, dict[Any, list[Any]]] | None = None
    try:
        snapshots = _snapshot_callback_registries(app)
        app.before_request(_begin_observed_request)
        _move_callback_to_start(
            app.before_request_funcs,
            _begin_observed_request,
        )
        app.after_request(_finish_observed_request)
        _move_callback_to_start(
            app.after_request_funcs,
            _finish_observed_request,
        )
        app.teardown_request(_close_observed_request)
        extensions[_EXTENSION_KEY] = runtime
    except Exception as exc:
        if snapshots is not None:
            _restore_callback_registries(app, snapshots, runtime)
        try:
            if existing is _MISSING:
                extensions.pop(_EXTENSION_KEY, None)
            else:
                extensions[_EXTENSION_KEY] = existing
        except Exception as rollback_error:
            runtime.diagnostics.report(
                "flask.callback_install_rollback", rollback_error
            )
        runtime.diagnostics.report("flask.callback_install", exc)

    return runtime


def _move_callback_to_start(
    registry: dict[Any, list[Any]], callback: Any
) -> None:
    callbacks = registry.get(None)
    if callbacks is None:
        raise RuntimeError("Flask did not register the callback.")
    for index, registered in enumerate(callbacks):
        if registered is callback:
            callbacks.insert(0, callbacks.pop(index))
            return
    raise RuntimeError("Flask did not register the callback.")


def _snapshot_callback_registries(
    app: Any,
) -> dict[str, dict[Any, list[Any]]]:
    return {
        name: {
            key: list(callbacks)
            for key, callbacks in getattr(app, name).items()
        }
        for name in _CALLBACK_REGISTRIES
    }


def _restore_callback_registries(
    app: Any,
    snapshots: dict[str, dict[Any, list[Any]]],
    runtime: ObservabilityRuntime,
) -> None:
    for name, snapshot in snapshots.items():
        try:
            registry = getattr(app, name)
            registry.clear()
            registry.update(
                {key: list(callbacks) for key, callbacks in snapshot.items()}
            )
        except Exception as exc:
            runtime.diagnostics.report(
                "flask.callback_install_rollback",
                exc,
                registry=name,
            )
