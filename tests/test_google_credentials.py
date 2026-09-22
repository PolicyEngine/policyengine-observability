from __future__ import annotations

import json

from policyengine_observability import google_credentials


def test_missing_and_malformed_credentials_are_nonfatal(monkeypatch) -> None:
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
    assert google_credentials.load_google_credentials() is None


def test_json_credentials_are_loaded_in_memory(monkeypatch) -> None:
    import google.auth

    config = {"type": "external_account", "audience": "test"}
    monkeypatch.setenv(
        "GCP_CREDENTIALS_JSON",
        json.dumps(config),
    )
    calls: dict[str, object] = {}
    marker = object()

    def load_credentials_from_dict(info, *, scopes):
        calls["info"] = info
        calls["scopes"] = scopes
        return marker, "project"

    monkeypatch.setattr(
        google.auth,
        "load_credentials_from_dict",
        load_credentials_from_dict,
    )
    assert google_credentials.load_google_credentials() is marker
    assert calls["info"] == config
    assert calls["scopes"] == list(google_credentials.GOOGLE_CREDENTIAL_SCOPES)


def test_modal_workload_identity_configuration(monkeypatch) -> None:
    from google.auth import identity_pool

    monkeypatch.setenv(
        google_credentials.MODAL_IDENTITY_TOKEN_ENV, "signed-test-token"
    )
    monkeypatch.setenv(
        google_credentials.WORKLOAD_IDENTITY_PROVIDER_ENV,
        "projects/123/locations/global/workloadIdentityPools/modal/providers/example",
    )
    monkeypatch.setenv(
        google_credentials.SERVICE_ACCOUNT_EMAIL_ENV,
        "modal-example@central.iam.gserviceaccount.com",
    )
    calls: dict[str, object] = {}

    def credentials(**kwargs):
        calls.update(kwargs)
        return "credentials"

    monkeypatch.setattr(identity_pool, "Credentials", credentials)
    config = google_credentials.load_google_credentials(
        prefer_workload_identity=True
    )
    assert config == "credentials"
    assert str(calls["audience"]).startswith(
        "//iam.googleapis.com/projects/123"
    )
    assert "modal-example@central.iam.gserviceaccount.com" in str(
        calls["service_account_impersonation_url"]
    )
    assert "credential_source" not in calls
    supplier = calls["subject_token_supplier"]
    assert supplier.get_subject_token(None, None) == "signed-test-token"


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
