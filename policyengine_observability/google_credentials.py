from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OIDC_TOKEN_ENV = "OBSERVABILITY_GOOGLE_OIDC_TOKEN"
MODAL_IDENTITY_TOKEN_ENV = "MODAL_IDENTITY_TOKEN"
WORKLOAD_IDENTITY_PROVIDER_ENV = (
    "OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER"
)
SERVICE_ACCOUNT_EMAIL_ENV = "OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL"
STS_TOKEN_URL_ENV = "OBSERVABILITY_GOOGLE_STS_TOKEN_URL"
DEFAULT_STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"
JWT_SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
GOOGLE_CREDENTIAL_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


@dataclass(frozen=True, slots=True)
class _EnvironmentSubjectTokenSupplier:
    env_names: tuple[str, ...]

    def get_subject_token(self, _context: Any, _request: Any) -> str:
        for name in self.env_names:
            token = os.getenv(name)
            if token:
                return token
        from google.auth.exceptions import RefreshError

        names = ", ".join(self.env_names)
        raise RefreshError(f"No workload identity token found in {names}.")


def load_google_credentials(
    *,
    credentials_json_env: str = "GCP_CREDENTIALS_JSON",
    application_credentials_env: str = "GOOGLE_APPLICATION_CREDENTIALS",
    prefer_workload_identity: bool = False,
) -> Any | None:
    """Load Google credentials without performing a network request."""

    try:
        if prefer_workload_identity:
            credentials = _workload_identity_credentials()
            if credentials is not None:
                return credentials

        configured_path = os.getenv(application_credentials_env)
        if configured_path:
            return _load_credentials_from_file(Path(configured_path))

        credentials_json = os.getenv(credentials_json_env)
        if credentials_json:
            return _load_credentials_from_json(credentials_json)

        if not prefer_workload_identity:
            return _workload_identity_credentials()
        return None
    except Exception:
        return None


def _load_credentials_from_json(credentials_json: str) -> Any:
    import google.auth

    config = json.loads(credentials_json)
    credentials, _project = google.auth.load_credentials_from_dict(
        config,
        scopes=list(GOOGLE_CREDENTIAL_SCOPES),
    )
    return credentials


def _workload_identity_credentials() -> Any | None:
    token_env_names = (OIDC_TOKEN_ENV, MODAL_IDENTITY_TOKEN_ENV)
    if not any(os.getenv(name) for name in token_env_names):
        return None
    provider = os.getenv(WORKLOAD_IDENTITY_PROVIDER_ENV)
    if not provider:
        return None

    from google.auth import identity_pool

    kwargs: dict[str, Any] = {
        "audience": _workload_identity_audience(provider),
        "subject_token_type": JWT_SUBJECT_TOKEN_TYPE,
        "token_url": os.getenv(STS_TOKEN_URL_ENV) or DEFAULT_STS_TOKEN_URL,
        "subject_token_supplier": _EnvironmentSubjectTokenSupplier(
            token_env_names
        ),
        "scopes": list(GOOGLE_CREDENTIAL_SCOPES),
    }
    service_account_email = os.getenv(SERVICE_ACCOUNT_EMAIL_ENV)
    if service_account_email:
        kwargs["service_account_impersonation_url"] = (
            "https://iamcredentials.googleapis.com/v1/projects/-/"
            f"serviceAccounts/{service_account_email}:generateAccessToken"
        )
    return identity_pool.Credentials(**kwargs)


def _load_credentials_from_file(path: Path) -> Any:
    config = json.loads(path.read_text())
    scopes = list(GOOGLE_CREDENTIAL_SCOPES)
    if config.get("type") == "external_account":
        from google.auth import identity_pool

        return identity_pool.Credentials.from_info(config, scopes=scopes)
    if config.get("type") == "service_account":
        from google.oauth2.service_account import Credentials

        return Credentials.from_service_account_file(str(path), scopes=scopes)

    import google.auth

    credentials, _project = google.auth.load_credentials_from_file(
        str(path), scopes=scopes
    )
    return credentials


def _workload_identity_audience(provider: str) -> str:
    value = provider.strip()
    if value.startswith("//iam.googleapis.com/"):
        return value
    if value.startswith("projects/"):
        return f"//iam.googleapis.com/{value}"
    return value
