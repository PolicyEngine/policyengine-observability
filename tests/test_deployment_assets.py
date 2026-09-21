from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
DEPLOY = ROOT / "deploy" / "gcp"


def test_dashboard_is_valid_json_with_required_signals() -> None:
    dashboard = json.loads((DEPLOY / "dashboard.json").read_text())
    serialized = json.dumps(dashboard)
    assert "policyengine.request.count" in serialized
    assert "policyengine.request.duration" in serialized
    assert "policyengine.error.count" in serialized
    assert "policyengine.telemetry.dropped" in serialized
    assert "policyengine.telemetry.exporter.failure" in serialized


def test_collector_accepts_only_traces_and_metrics() -> None:
    config = (DEPLOY / "collector" / "config.yaml").read_text()
    assert "telemetry.googleapis.com:443" in config
    assert "memory_limiter" in config
    assert "googleclientauth" in config
    assert "    traces:" in config
    assert "    metrics:" in config
    assert "    logs:\n      receivers:" not in config


def test_authorization_assets_exclude_unrelated_applications() -> None:
    iam = (DEPLOY / "iam.yaml").read_text()
    routing = (DEPLOY / "log-routing.yaml").read_text()
    for excluded in (
        "policyengine-household-api",
        "policyengine-uk-chat",
        "peukchat",
        "precompute",
        "smoke",
        "ephemeral",
    ):
        assert excluded not in iam
        assert excluded not in routing
    assert "policyengine-simulation-gateway" in iam
    assert "policyengine-simulation-py" in iam
    assert 'jsonPayload."service.namespace"' in routing


def test_verification_script_has_valid_shell_syntax() -> None:
    subprocess.run(
        ["bash", "-n", str(DEPLOY / "verify.sh")],
        check=True,
        capture_output=True,
        text=True,
    )
