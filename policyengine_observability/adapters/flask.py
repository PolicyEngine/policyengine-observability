from __future__ import annotations

import uuid
from typing import Any

from ..config import ObservabilityConfig
from ..context import RequestObservabilityContext
from ..runtime import OBSERVABILITY_INTERNAL_DISPATCH_HEADER
from ..runtime import REQUEST_ID_HEADER
from ..runtime import ObservabilityRuntime
from ..runtime import set_observability_runtime


class FlaskObservabilityAdapter:
    def __init__(self, runtime: ObservabilityRuntime) -> None:
        self.runtime = runtime

    def instrument_app(self, app: Any) -> None:
        if not self.runtime.enabled:
            return
        if app.extensions.get("policyengine_observability_adapter"):
            return
        app.extensions["policyengine_observability_adapter"] = self

        @app.before_request
        def _start_observed_request() -> None:
            self.start_request()

        @app.after_request
        def _finish_observed_request(response):
            headers = self.runtime.finish_request(response.status_code)
            for key, value in headers.items():
                response.headers[key] = value
            return response

        @app.teardown_request
        def _emit_observed_request(exc) -> None:
            self.runtime.teardown_request(exc)

    def start_request(self) -> None:
        try:
            from flask import request

            route = request.url_rule.rule if request.url_rule else request.path
            request_id = request.headers.get(REQUEST_ID_HEADER) or str(
                uuid.uuid4()
            )
            context = RequestObservabilityContext(
                config=self.runtime.config,
                request_id=request_id,
                method=request.method,
                route=route,
                path=request.path,
                endpoint=request.endpoint,
                query_keys=sorted(request.args.keys()),
                content_length_bytes=request.content_length,
                inbound=self._inbound_metadata(request),
                internal_dispatch=(
                    request.headers.get(OBSERVABILITY_INTERNAL_DISPATCH_HEADER)
                    == "1"
                ),
            )
            self.runtime.begin_request(context, carrier=request.headers)
        except BaseException as exc:
            self.runtime.log_observability_failure("flask.before_request", exc)

    def _inbound_metadata(self, request) -> dict:
        forwarded_for = _split_forwarded_for(
            request.headers.get("X-Forwarded-For")
        )
        x_real_ip = request.headers.get("X-Real-IP")
        remote_addr = request.remote_addr
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
            "user_agent": request.headers.get("User-Agent"),
            "origin": request.headers.get("Origin"),
            "referer": request.headers.get("Referer"),
            "host": request.host,
            "content_length_bytes": request.content_length,
        }
        if self.runtime.config.log_raw_ip:
            metadata["client_ip"] = client_ip
            metadata["forwarded_for"] = forwarded_for
            metadata["x_real_ip"] = x_real_ip
        return metadata


def init_flask_observability(
    app: Any,
    *,
    config: ObservabilityConfig | None = None,
    runtime: ObservabilityRuntime | None = None,
    service_name: str,
    service_role: str = "api",
    span_prefix: str | None = None,
    segment_registry=None,
) -> ObservabilityRuntime:
    if app.extensions.get("policyengine_observability"):
        return app.extensions["policyengine_observability"]
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
    app.extensions["policyengine_observability"] = runtime
    set_observability_runtime(runtime)
    FlaskObservabilityAdapter(runtime).instrument_app(app)
    return runtime


def _split_forwarded_for(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]
