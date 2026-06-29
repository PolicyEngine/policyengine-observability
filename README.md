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

Cloud Run captures stdout and stderr into Google Cloud Logging, so stdout is
usually the right destination for Cloud Run services. Non-GCP runtimes such as
Modal can write directly to Google Cloud Logging by installing the `google`
extra and enabling the Google destination:

```bash
OBSERVABILITY_LOG_DESTINATIONS=google_cloud_logging
OBSERVABILITY_GOOGLE_CLOUD_PROJECT=policyengine-observability
OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME=policyengine-observability
```

Multiple destinations can be enabled with a comma-separated list, for example
`OBSERVABILITY_LOG_DESTINATIONS=stdout,google_cloud_logging`. Google Cloud
Logging uses Application Default Credentials and requires permission to create
log entries, typically through `roles/logging.logWriter`.

## Release workflow

Changes should include a Towncrier fragment in `changelog.d/`. Pull requests
run changelog, Ruff, and coverage checks. Pushes to `main` run the same gates,
then publish a versioning commit that builds the changelog and bumps
`pyproject.toml`. That versioning commit publishes the package to PyPI through
trusted publishing, creates a matching git tag, and opens a GitHub release.
