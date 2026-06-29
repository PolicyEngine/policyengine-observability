from __future__ import annotations

import json
import os
import tempfile
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
GOOGLE_CREDENTIAL_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
)


def configure_google_application_credentials(
    *,
    credentials_json_env: str = "GCP_CREDENTIALS_JSON",
    application_credentials_env: str = "GOOGLE_APPLICATION_CREDENTIALS",
    credentials_path: Path | None = None,
) -> Path | None:
    try:
        existing_path = os.getenv(application_credentials_env)
        if existing_path:
            return Path(existing_path)

        path = _materialize_json_credentials(
            credentials_json_env=credentials_json_env,
            credentials_path=credentials_path,
        )
        if path is None:
            path = _materialize_workload_identity_credentials()
        if path is None:
            return None

        os.environ[application_credentials_env] = str(path)
        return path
    except Exception:
        return None


def load_google_credentials(
    *,
    credentials_json_env: str = "GCP_CREDENTIALS_JSON",
    application_credentials_env: str = "GOOGLE_APPLICATION_CREDENTIALS",
    credentials_path: Path | None = None,
    prefer_workload_identity: bool = False,
) -> Any | None:
    try:
        path: Path | None = None
        if prefer_workload_identity:
            path = _materialize_workload_identity_credentials()
        if path is None:
            existing_path = os.getenv(application_credentials_env)
            if existing_path:
                path = Path(existing_path)
        if path is None:
            path = _materialize_json_credentials(
                credentials_json_env=credentials_json_env,
                credentials_path=credentials_path,
            )
        if path is None and not prefer_workload_identity:
            path = _materialize_workload_identity_credentials()
        if path is None:
            return None

        return _load_credentials_from_file(path)
    except Exception:
        return None


def _materialize_json_credentials(
    *,
    credentials_json_env: str,
    credentials_path: Path | None,
) -> Path | None:
    credentials_json = os.getenv(credentials_json_env)
    if not credentials_json:
        return None

    json.loads(credentials_json)

    path = credentials_path or Path(tempfile.gettempdir()).joinpath(
        "policyengine-observability-gcp.json"
    )
    path.write_text(credentials_json)
    path.chmod(0o600)
    return path


def _materialize_workload_identity_credentials() -> Path | None:
    token = os.getenv(OIDC_TOKEN_ENV) or os.getenv(MODAL_IDENTITY_TOKEN_ENV)
    provider = os.getenv(WORKLOAD_IDENTITY_PROVIDER_ENV)
    if not token or not provider:
        return None

    directory = Path(tempfile.gettempdir())
    token_path = directory / "policyengine-observability-oidc.jwt"
    config_path = directory / "policyengine-observability-wif.json"

    token_path.write_text(token)
    token_path.chmod(0o600)
    config = _external_account_config(
        provider=provider,
        token_path=token_path,
        service_account_email=os.getenv(SERVICE_ACCOUNT_EMAIL_ENV),
        token_url=os.getenv(STS_TOKEN_URL_ENV) or DEFAULT_STS_TOKEN_URL,
    )
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    return config_path


def _load_credentials_from_file(path: Path) -> Any:
    config = json.loads(path.read_text())
    scopes = list(GOOGLE_CREDENTIAL_SCOPES)

    if config.get("type") == "external_account":
        from google.auth import identity_pool

        return identity_pool.Credentials.from_info(config, scopes=scopes)

    if config.get("type") == "service_account":
        from google.oauth2.service_account import Credentials

        return Credentials.from_service_account_file(
            str(path),
            scopes=scopes,
        )

    import google.auth

    credentials, _project = google.auth.load_credentials_from_file(
        str(path),
        scopes=scopes,
    )
    return credentials


def _external_account_config(
    *,
    provider: str,
    token_path: Path,
    service_account_email: str | None,
    token_url: str,
) -> dict[str, object]:
    config: dict[str, object] = {
        "type": "external_account",
        "audience": _workload_identity_audience(provider),
        "subject_token_type": JWT_SUBJECT_TOKEN_TYPE,
        "token_url": token_url,
        "credential_source": {
            "file": str(token_path),
            "format": {"type": "text"},
        },
    }
    if service_account_email:
        config["service_account_impersonation_url"] = (
            "https://iamcredentials.googleapis.com/v1/projects/-/"
            f"serviceAccounts/{service_account_email}:generateAccessToken"
        )
    return config


def _workload_identity_audience(provider: str) -> str:
    value = provider.strip()
    if value.startswith("//iam.googleapis.com/"):
        return value
    if value.startswith("projects/"):
        return f"//iam.googleapis.com/{value}"
    return value
