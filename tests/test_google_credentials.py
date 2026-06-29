from __future__ import annotations

import json
import os
import stat
import tempfile

import pytest

from policyengine_observability import google_credentials
from policyengine_observability.destinations import google_cloud_logging
from policyengine_observability.google_credentials import (
    configure_google_application_credentials,
    load_google_credentials,
)


@pytest.fixture(autouse=True)
def clear_google_credential_env(monkeypatch) -> None:
    for key in (
        "GCP_CREDENTIALS_JSON",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "MODAL_IDENTITY_TOKEN",
        "OBSERVABILITY_GOOGLE_OIDC_TOKEN",
        "OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL",
        "OBSERVABILITY_GOOGLE_STS_TOKEN_URL",
        "OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)


def test_configure_google_application_credentials_preserves_existing_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/existing.json")
    monkeypatch.setenv("GCP_CREDENTIALS_JSON", '{"project_id":"test"}')

    path = configure_google_application_credentials()

    assert str(path) == "/existing.json"
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/existing.json"


def test_configure_google_application_credentials_noops_without_json(
    monkeypatch,
) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_CREDENTIALS_JSON", raising=False)

    assert configure_google_application_credentials() is None
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


def test_configure_google_application_credentials_materializes_json(
    monkeypatch,
    tmp_path,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GCP_CREDENTIALS_JSON", '{"project_id":"test"}')

    path = configure_google_application_credentials(
        credentials_path=credentials_path,
    )

    assert path == credentials_path
    assert credentials_path.read_text() == '{"project_id":"test"}'
    assert stat.S_IMODE(credentials_path.stat().st_mode) == 0o600
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(
        credentials_path
    )


def test_configure_google_application_credentials_rejects_invalid_json(
    monkeypatch,
    tmp_path,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GCP_CREDENTIALS_JSON", "not-json")

    path = configure_google_application_credentials(
        credentials_path=credentials_path,
    )

    assert path is None
    assert not credentials_path.exists()
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


def test_configure_google_application_credentials_fails_open_on_unexpected_error(
    monkeypatch,
    tmp_path,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GCP_CREDENTIALS_JSON", '{"project_id":"test"}')
    monkeypatch.setattr(
        "policyengine_observability.google_credentials.json.loads",
        lambda _value: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    path = configure_google_application_credentials(
        credentials_path=credentials_path,
    )

    assert path is None
    assert not credentials_path.exists()
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


def test_configure_google_application_credentials_fails_open_on_write_error(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GCP_CREDENTIALS_JSON", '{"project_id":"test"}')

    path = configure_google_application_credentials(
        credentials_path=tmp_path / "missing" / "credentials.json",
    )

    assert path is None
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


def test_configure_google_application_credentials_materializes_oidc_wif(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_CREDENTIALS_JSON", raising=False)
    monkeypatch.setenv("OBSERVABILITY_GOOGLE_OIDC_TOKEN", "jwt-token")
    monkeypatch.setenv(
        "OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER",
        "projects/123/locations/global/workloadIdentityPools/modal/providers/modal",
    )
    monkeypatch.setenv(
        "OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL",
        "observability-writer@example.iam.gserviceaccount.com",
    )

    path = configure_google_application_credentials()

    assert path == tmp_path / "policyengine-observability-wif.json"
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    token_path = tmp_path / "policyengine-observability-oidc.jwt"
    assert token_path.read_text() == "jwt-token"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    config = json.loads(path.read_text())
    assert config == {
        "type": "external_account",
        "audience": (
            "//iam.googleapis.com/projects/123/locations/global/"
            "workloadIdentityPools/modal/providers/modal"
        ),
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "credential_source": {
            "file": str(token_path),
            "format": {"type": "text"},
        },
        "service_account_impersonation_url": (
            "https://iamcredentials.googleapis.com/v1/projects/-/"
            "serviceAccounts/observability-writer@example.iam.gserviceaccount.com"
            ":generateAccessToken"
        ),
    }


def test_configure_google_application_credentials_preserves_full_wif_audience(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_CREDENTIALS_JSON", raising=False)
    monkeypatch.setenv("OBSERVABILITY_GOOGLE_OIDC_TOKEN", "jwt-token")
    monkeypatch.setenv(
        "OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER",
        "//iam.googleapis.com/projects/123/locations/global/"
        "workloadIdentityPools/modal/providers/modal",
    )

    path = configure_google_application_credentials()

    config = json.loads(path.read_text())
    assert config["audience"] == (
        "//iam.googleapis.com/projects/123/locations/global/"
        "workloadIdentityPools/modal/providers/modal"
    )
    assert "service_account_impersonation_url" not in config


def test_configure_google_application_credentials_uses_modal_identity_token(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("OBSERVABILITY_GOOGLE_OIDC_TOKEN", raising=False)
    monkeypatch.setenv("MODAL_IDENTITY_TOKEN", "modal-jwt-token")
    monkeypatch.setenv(
        "OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER",
        "projects/123/locations/global/workloadIdentityPools/modal/providers/modal",
    )

    path = configure_google_application_credentials()

    assert path == tmp_path / "policyengine-observability-wif.json"
    token_path = tmp_path / "policyengine-observability-oidc.jwt"
    assert token_path.read_text() == "modal-jwt-token"


def test_load_google_credentials_prefers_wif_without_mutating_adc(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/analytics.json")
    monkeypatch.setenv("GCP_CREDENTIALS_JSON", '{"project_id":"analytics"}')
    monkeypatch.setenv("MODAL_IDENTITY_TOKEN", "modal-jwt-token")
    monkeypatch.setenv(
        "OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER",
        "projects/123/locations/global/workloadIdentityPools/modal/providers/modal",
    )
    calls = []
    monkeypatch.setattr(
        google_credentials,
        "_load_credentials_from_file",
        lambda path: calls.append(path) or "wif-credentials",
    )

    credentials = load_google_credentials(prefer_workload_identity=True)

    assert credentials == "wif-credentials"
    assert calls == [tmp_path / "policyengine-observability-wif.json"]
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/analytics.json"


def test_load_google_credentials_loads_identity_pool_credentials(
    monkeypatch,
    tmp_path,
) -> None:
    from google.auth import identity_pool

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_CREDENTIALS_JSON", raising=False)
    monkeypatch.setenv("MODAL_IDENTITY_TOKEN", "modal-jwt-token")
    monkeypatch.setenv(
        "OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER",
        "projects/123/locations/global/workloadIdentityPools/modal/providers/modal",
    )

    credentials = load_google_credentials(prefer_workload_identity=True)

    assert isinstance(credentials, identity_pool.Credentials)
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


def test_google_destination_bootstraps_application_credentials(
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        google_cloud_logging,
        "configure_google_application_credentials",
        lambda: calls.append("configured"),
    )
    monkeypatch.setattr(
        google_cloud_logging,
        "load_google_credentials",
        lambda *, prefer_workload_identity: None,
    )

    class FakeClient:
        project = "test-project"

        def logger(self, log_name):
            return log_name

    destination = google_cloud_logging.GoogleCloudLoggingDestination(
        project=None,
        log_name="policyengine-observability",
        client_factory=lambda _project, _credentials: FakeClient(),
    )

    assert calls == ["configured"]
    assert destination.project == "test-project"
    assert destination.logger == "policyengine-observability"


def test_google_destination_passes_loaded_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        google_cloud_logging,
        "load_google_credentials",
        lambda *, prefer_workload_identity: "wif-credentials",
    )

    calls = []

    class FakeClient:
        project = "test-project"

        def logger(self, log_name):
            return log_name

    destination = google_cloud_logging.GoogleCloudLoggingDestination(
        project="central-project",
        log_name="policyengine-observability",
        client_factory=lambda project, credentials: calls.append(
            (project, credentials)
        )
        or FakeClient(),
    )

    assert calls == [("central-project", "wif-credentials")]
    assert destination.logger == "policyengine-observability"
