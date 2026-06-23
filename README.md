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

## Release workflow

Changes should include a Towncrier fragment in `changelog.d/`. Pull requests
run changelog, Ruff, and coverage checks. Pushes to `main` run the same gates,
then publish a versioning commit that builds the changelog and bumps
`pyproject.toml`. That versioning commit publishes the package to PyPI through
trusted publishing, creates a matching git tag, and opens a GitHub release.
