from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .config import OTLPProtocol
from .google_credentials import (
    DEFAULT_STS_TOKEN_URL,
    GOOGLE_CREDENTIAL_SCOPES,
    JWT_SUBJECT_TOKEN_TYPE,
    MODAL_IDENTITY_TOKEN_ENV,
    OIDC_TOKEN_ENV,
    SERVICE_ACCOUNT_EMAIL_ENV,
    WORKLOAD_IDENTITY_PROVIDER_ENV,
    _EnvironmentSubjectTokenSupplier,
    _workload_identity_audience,
)


@dataclass(frozen=True, slots=True)
class GoogleIdTokenAuth:
    """Authenticates an OTLP exporter to an ID-token protected endpoint."""

    audience: str

    def exporter_kwargs(
        self,
        *,
        protocol: OTLPProtocol,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        if protocol == "grpc":
            return {"credentials": _google_grpc_credentials(self.audience)}
        return {"session": _google_http_session(self.audience, headers)}


def _google_id_token_credentials(audience: str) -> Any:
    from google.auth.transport.requests import Request

    token_env_names = (MODAL_IDENTITY_TOKEN_ENV, OIDC_TOKEN_ENV)
    has_subject_token = any(os.getenv(name) for name in token_env_names)
    provider = os.getenv(WORKLOAD_IDENTITY_PROVIDER_ENV)
    service_account = os.getenv(SERVICE_ACCOUNT_EMAIL_ENV)
    if has_subject_token and provider and service_account:
        from google.auth import identity_pool, impersonated_credentials

        source = identity_pool.Credentials(
            audience=_workload_identity_audience(provider),
            subject_token_type=JWT_SUBJECT_TOKEN_TYPE,
            token_url=DEFAULT_STS_TOKEN_URL,
            subject_token_supplier=_EnvironmentSubjectTokenSupplier(
                token_env_names
            ),
            scopes=list(GOOGLE_CREDENTIAL_SCOPES),
        )
        target = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=service_account,
            target_scopes=list(GOOGLE_CREDENTIAL_SCOPES),
        )
        return impersonated_credentials.IDTokenCredentials(
            target_credentials=target,
            target_audience=audience,
            include_email=True,
        )

    from google.oauth2.id_token import fetch_id_token_credentials

    return fetch_id_token_credentials(audience, request=Request())


def _google_grpc_credentials(audience: str) -> Any:
    import grpc
    from google.auth.transport.grpc import AuthMetadataPlugin
    from google.auth.transport.requests import Request

    plugin = AuthMetadataPlugin(
        _google_id_token_credentials(audience),
        Request(),
        default_host=urlparse(audience).netloc,
    )
    return grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(),
        grpc.metadata_call_credentials(plugin),
    )


def _google_http_session(audience: str, headers: dict[str, str]) -> Any:
    from google.auth.transport.requests import AuthorizedSession

    session = AuthorizedSession(_google_id_token_credentials(audience))
    session.headers.update(headers)
    return session
