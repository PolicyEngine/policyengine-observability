from __future__ import annotations

import json
import stat

from policyengine_observability import google_credentials


def test_missing_and_malformed_credentials_are_nonfatal(
    monkeypatch, tmp_path
) -> None:
    for name in (
        "GCP_CREDENTIALS_JSON",
        "GOOGLE_APPLICATION_CREDENTIALS",
        google_credentials.MODAL_IDENTITY_TOKEN_ENV,
        google_credentials.OIDC_TOKEN_ENV,
        google_credentials.WORKLOAD_IDENTITY_PROVIDER_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    assert google_credentials.load_google_credentials() is None
    monkeypatch.setenv("GCP_CREDENTIALS_JSON", "not-json")
    assert (
        google_credentials.load_google_credentials(
            credentials_path=tmp_path / "credentials.json"
        )
        is None
    )


def test_json_credentials_are_materialized_with_private_permissions(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "credentials.json"
    monkeypatch.setenv(
        "GCP_CREDENTIALS_JSON",
        json.dumps({"type": "external_account", "audience": "test"}),
    )
    monkeypatch.setattr(
        google_credentials,
        "_load_credentials_from_file",
        lambda loaded_path: loaded_path,
    )
    result = google_credentials.load_google_credentials(credentials_path=path)
    assert result == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_modal_workload_identity_configuration(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        google_credentials.tempfile, "gettempdir", lambda: str(tmp_path)
    )
    monkeypatch.setenv(
        google_credentials.MODAL_IDENTITY_TOKEN_ENV, "signed-test-token"
    )
    monkeypatch.setenv(
        google_credentials.WORKLOAD_IDENTITY_PROVIDER_ENV,
        "projects/123/locations/global/workloadIdentityPools/modal/providers/api-v1",
    )
    monkeypatch.setenv(
        google_credentials.SERVICE_ACCOUNT_EMAIL_ENV,
        "modal-api-v1@central.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(
        google_credentials,
        "_load_credentials_from_file",
        lambda path: json.loads(path.read_text()),
    )
    config = google_credentials.load_google_credentials(
        prefer_workload_identity=True
    )
    assert config["audience"].startswith("//iam.googleapis.com/projects/123")
    assert (
        "modal-api-v1@central.iam.gserviceaccount.com"
        in config["service_account_impersonation_url"]
    )
    token_path = tmp_path / "policyengine-observability-oidc.jwt"
    assert token_path.read_text() == "signed-test-token"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_existing_credentials_path_is_loaded(monkeypatch, tmp_path) -> None:
    path = tmp_path / "adc.json"
    path.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    marker = object()
    monkeypatch.setattr(
        google_credentials,
        "_load_credentials_from_file",
        lambda loaded: marker if loaded == path else None,
    )
    assert google_credentials.load_google_credentials() is marker


def test_workload_identity_audience_preserves_supported_forms() -> None:
    full = "//iam.googleapis.com/projects/123/providers/test"
    assert google_credentials._workload_identity_audience(full) == full
    assert (
        google_credentials._workload_identity_audience(
            "projects/123/providers/test"
        )
        == "//iam.googleapis.com/projects/123/providers/test"
    )
    assert google_credentials._workload_identity_audience("custom") == "custom"
