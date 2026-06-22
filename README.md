# policyengine-observability

Shared PolicyEngine observability runtime for fail-open local timings,
structured logs, OpenTelemetry traces, and OpenTelemetry metrics.

The package intentionally keeps framework support in adapters:

- `policyengine_observability.adapters.flask`
- `policyengine_observability.adapters.fastapi`
- `policyengine_observability.integrations.httpx`

OpenTelemetry imports are lazy. Timing and structured logging can run without
an OTel backend; exporting traces/metrics is opt-in through configuration.

## Release workflow

Changes should include a Towncrier fragment in `changelog.d/`. Pull requests
run changelog, Ruff, and coverage checks. Pushes to `main` run the same gates,
then publish a versioning commit that builds the changelog and bumps
`pyproject.toml`. That versioning commit publishes the package to PyPI through
trusted publishing, creates a matching git tag, and opens a GitHub release.
