from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import OTLPProtocol


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

    modal_token = os.getenv("MODAL_IDENTITY_TOKEN") or os.getenv(
        "OBSERVABILITY_GOOGLE_OIDC_TOKEN"
    )
    provider = os.getenv("OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER")
    service_account = os.getenv("OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL")
    if modal_token and provider and service_account:
        from google.auth import identity_pool, impersonated_credentials

        source = identity_pool.Credentials.from_info(
            {
                "type": "external_account",
                "audience": _workload_identity_audience(provider),
                "subject_token_type": ("urn:ietf:params:oauth:token-type:jwt"),
                "token_url": "https://sts.googleapis.com/v1/token",
                "credential_source": {
                    "file": _write_subject_token(modal_token),
                    "format": {"type": "text"},
                },
            },
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        target = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=service_account,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
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


def _workload_identity_audience(provider: str) -> str:
    value = provider.strip()
    if value.startswith("//iam.googleapis.com/"):
        return value
    if value.startswith("projects/"):
        return f"//iam.googleapis.com/{value}"
    return value


def _write_subject_token(token: str) -> str:
    path = Path(tempfile.gettempdir()) / "policyengine-observability-otel.jwt"
    path.write_text(token)
    path.chmod(0o600)
    return str(path)
