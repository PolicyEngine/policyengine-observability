"""Deprecated import location kept for backward compatibility.

The Google credential helpers are edge (backend-specific) code and live
in ``policyengine_observability.destinations.google_credentials``.
Import them from there or from the package top level.
"""

from __future__ import annotations

from .destinations.google_credentials import (
    DEFAULT_STS_TOKEN_URL,
    GOOGLE_CREDENTIAL_SCOPES,
    JWT_SUBJECT_TOKEN_TYPE,
    MODAL_IDENTITY_TOKEN_ENV,
    OIDC_TOKEN_ENV,
    SERVICE_ACCOUNT_EMAIL_ENV,
    STS_TOKEN_URL_ENV,
    WORKLOAD_IDENTITY_PROVIDER_ENV,
    configure_google_application_credentials,
    load_google_credentials,
)

__all__ = [
    "DEFAULT_STS_TOKEN_URL",
    "GOOGLE_CREDENTIAL_SCOPES",
    "JWT_SUBJECT_TOKEN_TYPE",
    "MODAL_IDENTITY_TOKEN_ENV",
    "OIDC_TOKEN_ENV",
    "SERVICE_ACCOUNT_EMAIL_ENV",
    "STS_TOKEN_URL_ENV",
    "WORKLOAD_IDENTITY_PROVIDER_ENV",
    "configure_google_application_credentials",
    "load_google_credentials",
]
