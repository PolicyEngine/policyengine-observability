# Repository Guidance

Use this skill when making or reviewing repository-wide API, testing,
documentation, release, or package-boundary changes.

## Commands

```bash
uv sync --extra dev --extra all
uv run --extra dev ruff format .
uv run --extra dev ruff format --check .
uv run --extra dev ruff check .
uv run --extra dev --extra all coverage run -m pytest
uv run --extra dev --extra all coverage report
uv build
```

To check for a changelog fragment locally:

```bash
uv run --extra dev towncrier check --compare-with origin/main
```

## What Lives Here

- `policyengine_observability/config.py` resolves environment-driven runtime
  configuration.
- `policyengine_observability/context.py` defines request and operation log
  payload structures.
- `policyengine_observability/runtime.py` owns context management, segments,
  structured logs, metrics, traces, events, and fail-open behavior.
- `policyengine_observability/adapters/` contains framework adapters such as
  Flask and FastAPI.
- `policyengine_observability/integrations/` contains optional integrations
  such as HTTP client instrumentation.
- `.github/` contains changelog, versioning, tagging, and PyPI publication
  automation.
- `tests/` should cover runtime behavior, framework adapters, public exports,
  and release scripts.

## Design Boundaries

- Keep the core runtime framework-agnostic. HTTP frameworks belong in adapters.
- Keep the context-manager API usable from HTTP requests, worker functions,
  CLI scripts, and tests.
- Keep OpenTelemetry optional and lazily imported. Timing and structured
  logging must work without an OTel backend.
- Observability failures must fail open: record an internal observability error
  when practical, but do not break the application operation being observed.
- Preserve structured log schemas. Make additive changes when possible; bump
  schema versions for breaking payload changes.
- Keep metric attributes bounded and low-cardinality. Do not put raw paths,
  full URLs, request bodies, or unbounded user-provided values into metric
  labels.
- Keep segment names stable. Prefer registered segment enums in consuming
  applications, while preserving safe string fallback behavior.

## Testing

Add focused tests for runtime context behavior and failure paths whenever
changing `runtime.py`. Adapter changes should include framework-level tests that
exercise request setup, response headers, error paths, and teardown behavior.

Release automation changes should include tests for the helper scripts when the
logic is non-trivial.

## Release Expectations

Every behavior change should include a Towncrier fragment in `changelog.d/`.
The push workflow builds the changelog, bumps the package version, tags the
release, and publishes to PyPI through trusted publishing.

## Anti-Patterns

- Do not add a hard dependency on a specific observability vendor.
- Do not require OTel configuration for logs or timings to function.
- Do not duplicate framework behavior in the core runtime.
- Do not swallow application exceptions from observed code.
- Do not use `[codex]`, `[claude]`, `[copilot]`, or other agent labels in PR
  titles.
