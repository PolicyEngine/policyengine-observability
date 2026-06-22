from __future__ import annotations

import uuid
from typing import Any

from ..config import ObservabilityConfig
from ..context import RequestObservabilityContext
from ..runtime import REQUEST_ID_HEADER
from ..runtime import ObservabilityRuntime
from ..runtime import set_observability_runtime


class FastAPIObservabilityAdapter:
    def __init__(self, runtime: ObservabilityRuntime) -> None:
        self.runtime = runtime

    def instrument_app(self, app: Any) -> None:
        if not self.runtime.enabled:
            return
        if getattr(app.state, "policyengine_observability_adapter", None):
            return
        app.state.policyengine_observability_adapter = self
        if self.runtime.config.instrument_fastapi:
            self._instrument_fastapi(app)

        @app.middleware("http")
        async def _observe_request(request, call_next):
            self.start_request(request)
            response = None
            caught = None
            try:
                response = await call_next(request)
                headers = self.runtime.finish_request(response.status_code)
                for key, value in headers.items():
                    response.headers[key] = value
                return response
            except BaseException as exc:
                caught = exc
                raise
            finally:
                self.runtime.teardown_request(caught)

    def start_request(self, request) -> None:
        try:
            route = getattr(request.scope.get("route"), "path", None)
            path = request.url.path
            request_id = request.headers.get(REQUEST_ID_HEADER) or str(
                uuid.uuid4()
            )
            context = RequestObservabilityContext(
                config=self.runtime.config,
                request_id=request_id,
                method=request.method,
                route=route or path,
                path=path,
                endpoint=route,
                query_keys=sorted(request.query_params.keys()),
                content_length_bytes=_int_header(
                    request.headers.get("content-length")
                ),
                inbound=self._inbound_metadata(request),
            )
            self.runtime.begin_request(context, carrier=request.headers)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "fastapi.before_request",
                exc,
            )

    def _instrument_fastapi(self, app: Any) -> None:
        try:
            from opentelemetry.instrumentation.fastapi import (
                FastAPIInstrumentor,
            )

            FastAPIInstrumentor.instrument_app(app)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "fastapi.auto_instrument",
                exc,
            )

    def _inbound_metadata(self, request) -> dict:
        forwarded_for = _split_forwarded_for(
            request.headers.get("x-forwarded-for")
        )
        x_real_ip = request.headers.get("x-real-ip")
        remote_addr = request.client.host if request.client else None
        client_ip = None
        ip_source = None
        if forwarded_for:
            client_ip = forwarded_for[0]
            ip_source = "x_forwarded_for"
        elif x_real_ip:
            client_ip = x_real_ip
            ip_source = "x_real_ip"
        elif remote_addr:
            client_ip = remote_addr
            ip_source = "remote_addr"
        metadata = {
            "ip_source": ip_source,
            "user_agent": request.headers.get("user-agent"),
            "origin": request.headers.get("origin"),
            "referer": request.headers.get("referer"),
            "host": request.headers.get("host"),
            "content_length_bytes": _int_header(
                request.headers.get("content-length")
            ),
        }
        if self.runtime.config.log_raw_ip:
            metadata["client_ip"] = client_ip
            metadata["forwarded_for"] = forwarded_for
            metadata["x_real_ip"] = x_real_ip
        return metadata


def init_fastapi_observability(
    app: Any,
    *,
    config: ObservabilityConfig | None = None,
    runtime: ObservabilityRuntime | None = None,
    service_name: str,
    service_role: str = "api",
    span_prefix: str | None = None,
    segment_registry=None,
) -> ObservabilityRuntime:
    existing = getattr(app.state, "policyengine_observability", None)
    if existing:
        return existing
    runtime = runtime or ObservabilityRuntime(
        config
        or ObservabilityConfig.from_env(
            service_name=service_name,
            service_role=service_role,
            span_prefix=span_prefix,
        ),
        segment_registry=segment_registry,
    )
    runtime.configure()
    app.state.policyengine_observability = runtime
    set_observability_runtime(runtime)
    FastAPIObservabilityAdapter(runtime).instrument_app(app)
    return runtime


def _split_forwarded_for(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _int_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
