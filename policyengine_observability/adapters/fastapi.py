from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import parse_qs

from ..config import ObservabilityConfig
from ..context import RequestObservabilityContext
from ..runtime import (
    REQUEST_ID_HEADER,
    TRACEPARENT_HEADER,
    ObservabilityRuntime,
    set_observability_runtime,
)

UNMATCHED_ROUTE = "<unmatched>"


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
            self.runtime.instrument_fastapi(app)
        try:
            app.add_middleware(
                FastAPIObservabilityMiddleware,
                adapter=self,
            )
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "fastapi.middleware_install",
                exc,
            )

    def start_request(self, scope: dict[str, Any]) -> None:
        try:
            headers = _headers_from_scope(scope)
            path = scope.get("path") or ""
            route = _route_from_scope(scope) or UNMATCHED_ROUTE
            endpoint = _endpoint_from_scope(scope)
            request_id = headers.get(REQUEST_ID_HEADER.lower()) or str(
                uuid.uuid4()
            )
            context = RequestObservabilityContext(
                config=self.runtime.config,
                request_id=request_id,
                method=scope.get("method") or "",
                route=route,
                path=path,
                endpoint=endpoint,
                query_keys=_query_keys(scope),
                content_length_bytes=_int_header(
                    headers.get("content-length")
                ),
                inbound=self._inbound_metadata(scope, headers),
            )
            self.runtime.begin_request(context, carrier=headers)
        except BaseException as exc:
            self.runtime.log_observability_failure(
                "fastapi.before_request",
                exc,
            )

    def update_resolved_route(self, scope: dict[str, Any]) -> None:
        route = _route_from_scope(scope)
        endpoint = _endpoint_from_scope(scope)
        if route or endpoint:
            self.runtime.update_request_route(route=route, endpoint=endpoint)

    def _inbound_metadata(
        self,
        scope: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        forwarded_for = _split_forwarded_for(headers.get("x-forwarded-for"))
        x_real_ip = headers.get("x-real-ip")
        client = scope.get("client") or ()
        remote_addr = client[0] if client else None
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
            "user_agent": headers.get("user-agent"),
            "origin": headers.get("origin"),
            "referer": headers.get("referer"),
            "host": headers.get("host"),
            "content_length_bytes": _int_header(headers.get("content-length")),
        }
        if self.runtime.config.log_raw_ip:
            metadata["client_ip"] = client_ip
            metadata["forwarded_for"] = forwarded_for
            metadata["x_real_ip"] = x_real_ip
        return metadata


class FastAPIObservabilityMiddleware:
    def __init__(
        self,
        app: Any,
        *,
        adapter: FastAPIObservabilityAdapter,
    ) -> None:
        self.app = app
        self.adapter = adapter

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        self.adapter.start_request(scope)
        status_code: int | None = None
        completed = False

        async def send_wrapper(message) -> None:
            nonlocal completed
            nonlocal status_code

            if message["type"] == "http.response.start":
                status_code = int(message.get("status") or 0)
                self.adapter.update_resolved_route(scope)
                response_headers = self.adapter.runtime.prepare_response(
                    status_code
                )
                if response_headers:
                    message = {
                        **message,
                        "headers": _merge_response_headers(
                            message.get("headers") or [],
                            response_headers,
                        ),
                    }
                await send(message)
                return

            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                try:
                    await send(message)
                finally:
                    completed = True
                    self.adapter.update_resolved_route(scope)
                    self.adapter.runtime.complete_request(status_code)
                    self.adapter.runtime.teardown_request(None)
                return

            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:
            if not completed:
                error_status = status_code or 500
                self.adapter.update_resolved_route(scope)
                self.adapter.runtime.prepare_response(error_status)
                self.adapter.runtime.complete_request(error_status)
                self.adapter.runtime.teardown_request(exc)
                completed = True
            raise
        finally:
            if not completed:
                self.adapter.update_resolved_route(scope)
                self.adapter.runtime.complete_request(status_code)
                self.adapter.runtime.teardown_request(None)


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


def _headers_from_scope(scope: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        try:
            header_key = key.decode("latin-1").lower()
            header_value = value.decode("latin-1")
        except BaseException:
            continue
        if header_key in headers:
            headers[header_key] = f"{headers[header_key]},{header_value}"
        else:
            headers[header_key] = header_value
    return headers


def _route_from_scope(scope: dict[str, Any]) -> str | None:
    route = scope.get("route")
    route_path = getattr(route, "path", None)
    if route_path:
        return str(route_path)
    return None


def _endpoint_from_scope(scope: dict[str, Any]) -> str | None:
    endpoint = scope.get("endpoint")
    if endpoint is None:
        return None
    endpoint_name = getattr(endpoint, "__name__", None)
    return endpoint_name or str(endpoint)


def _query_keys(scope: dict[str, Any]) -> list[str]:
    query_string = scope.get("query_string") or b""
    try:
        decoded = query_string.decode("latin-1")
    except AttributeError:
        decoded = str(query_string)
    return sorted(parse_qs(decoded, keep_blank_values=True).keys())


def _merge_response_headers(
    existing_headers: list[tuple[bytes, bytes]],
    headers: dict[str, str],
) -> list[tuple[bytes, bytes]]:
    response_header_names = {key.lower().encode("latin-1") for key in headers}
    merged = [
        (key, value)
        for key, value in existing_headers
        if key.lower() not in response_header_names
    ]
    merged.extend(
        (
            key.encode("latin-1"),
            value.encode("latin-1"),
        )
        for key, value in headers.items()
        if key in {REQUEST_ID_HEADER, TRACEPARENT_HEADER}
    )
    return merged


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
