# policyengine-observability

Shared PolicyEngine observability runtime for fail-open local timings,
structured logs, OpenTelemetry traces, and OpenTelemetry metrics.

The package intentionally keeps framework support in adapters:

- `policyengine_observability.adapters.flask`
- `policyengine_observability.adapters.fastapi`
- `policyengine_observability.integrations.httpx`

OpenTelemetry support is installed and enabled by default. Timing and
structured logging run even without an OTLP collector; when no endpoint is
configured, spans and metrics stay in-process while logs still receive trace
context. Set `OTEL_ENABLED=false` to opt out. Configure
`OTEL_EXPORTER_OTLP_ENDPOINT` to export traces and metrics.

Structured logs write to stdout by default:

```bash
OBSERVABILITY_LOG_DESTINATIONS=stdout
```

Applications can override that default in code when constructing
`ObservabilityConfig`:

```python
ObservabilityConfig.from_env(
    service_name="policyengine-api",
    default_log_destinations=("google_cloud_logging",),
)
```

`OBSERVABILITY_LOG_DESTINATIONS` still has precedence over application
defaults. Cloud Run captures stdout and stderr into Google Cloud Logging
automatically, but applications that need one consistent destination across
Cloud Run and non-GCP runtimes can write directly to Google Cloud Logging by
installing the `google` extra and enabling the Google destination:

```bash
OBSERVABILITY_LOG_DESTINATIONS=google_cloud_logging
OBSERVABILITY_GOOGLE_CLOUD_PROJECT=policyengine-api
OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME=policyengine-observability
```

Multiple destinations can be enabled with a comma-separated list, for example
`OBSERVABILITY_LOG_DESTINATIONS=stdout,google_cloud_logging`. Google Cloud
Logging uses Application Default Credentials and requires permission to create
log entries, typically through `roles/logging.logWriter`.

Request and operation logs include two timing views:

- `timings_ms` and `timing_counts` are flat inclusive aggregates by segment
  name, intended for quick scanning and compatibility with existing log
  queries.
- `segment_tree` is an ordered nested view of segment occurrences. Repeated
  sibling segments are preserved as separate entries, and safe scalar segment
  attributes are included so callers can distinguish settings such as
  `simulation_kind=baseline` versus `simulation_kind=reform`.

Core structured log fields take precedence over caller-provided attributes with
the same keys.

On runtimes that do not provide Application Default Credentials, set
`GCP_CREDENTIALS_JSON` to a service account JSON document. The Google Cloud
Logging destination will materialize it into a temporary credentials file and
pass those credentials directly to the Google client. If the credential
bootstrap fails, observability fails open and continues without raising into
application code.

Prefer OIDC-based Workload Identity Federation over long-lived service account
keys when the runtime can provide an OIDC subject token. Modal injects
generated identity tokens into Function containers through
`MODAL_IDENTITY_TOKEN`; other runtimes can provide
`OBSERVABILITY_GOOGLE_OIDC_TOKEN`. The runtime needs these values:

```bash
OBSERVABILITY_GOOGLE_OIDC_TOKEN=OIDC_TOKEN_FROM_RUNTIME
OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER=projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL_ID/providers/PROVIDER_ID
OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL=observability-writer@PROJECT_ID.iam.gserviceaccount.com
OBSERVABILITY_GOOGLE_CLOUD_PROJECT=PROJECT_ID
```

When `MODAL_IDENTITY_TOKEN` or `OBSERVABILITY_GOOGLE_OIDC_TOKEN` is present
alongside `OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER`, the Google Cloud
Logging destination writes a temporary external-account credential
configuration and passes those credentials directly to the Cloud Logging
client. If `OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL` is present, the
configuration uses service account impersonation. This keeps observability
credentials separate from any application-level `GOOGLE_APPLICATION_CREDENTIALS`
or `GCP_CREDENTIALS_JSON` used by the service for other Google clients.

The Google Cloud setup needs:

- A Workload Identity Pool and OIDC provider whose issuer matches Modal's OIDC
  issuer, `https://oidc.modal.com`.
- Attribute mapping for the Modal token claims you want to authorize, such as
  `google.subject=assertion.sub`.
- A service account with `roles/logging.logWriter` on the log project.
- An IAM binding granting the workload identity principal
  `roles/iam.workloadIdentityUser` on that service account.

For the fixed PolicyEngine Google Cloud destination, see
[`docs/operations/google-cloud-stage3-runbook.md`](docs/operations/google-cloud-stage3-runbook.md).

## Release workflow

Changes should include a Towncrier fragment in `changelog.d/`. Pull requests
run changelog, Ruff, and coverage checks. Pushes to `main` run the same gates,
then publish a versioning commit that builds the changelog and bumps
`pyproject.toml`. That versioning commit publishes the package to PyPI through
trusted publishing, creates a matching git tag, and opens a GitHub release.
