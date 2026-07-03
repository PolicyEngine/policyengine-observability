## [1.3.2] - 2026-07-03

### Fixed

- Disable a log destination after repeated consecutive emit failures, restoring stdout if none remain — an observability sink can no longer degrade its host service's request path.


## [1.3.1] - 2026-07-02

### Fixed

- Set coherent minimum versions across the OpenTelemetry dependency stack: `opentelemetry-api`, `-sdk`, `-exporter-otlp-proto-grpc`, and `-exporter-otlp-proto-http` to `>=1.43.0`, and `opentelemetry-instrumentation-fastapi` and `-httpx` to `>=0.64b0`. These forward-pin the whole stack to the coordinated OpenTelemetry release that fixes `AttributeError: '_IncludedRouter' object has no attribute 'path'` — which earlier `opentelemetry-instrumentation-fastapi` raised on FastAPI >= 0.137 `include_router` routing, turning every CORS preflight `OPTIONS` request into a 500 for consumers that enable FastAPI instrumentation.


## [1.3.0] - 2026-07-01

### Added

- Add ordered nested segment trees to request and operation structured logs.
  Core structured log fields now take precedence over caller-provided attributes.


## [1.2.1] - 2026-07-01

### Changed

- Document the fixed Google Cloud Stage 3 observability destination, including Cloud Run routing, Modal WIF, and smoke-test queries.


## [1.2.0] - 2026-06-29

### Added

- Add shared Google Cloud Logging credential bootstrap and consumer-configurable default log destinations.


## [1.1.0] - 2026-06-29

### Added

- Add optional Google Cloud Logging log destinations for structured observability records.


## [1.0.0] - 2026-06-23

### Breaking changes

- Simplified OpenTelemetry enablement so `OTEL_ENABLED` is the only environment
  switch used by `ObservabilityConfig.from_env`.


## [0.4.1] - 2026-06-23

### Changed

- Install and enable OpenTelemetry dependencies by default, while preserving env-based opt-out.


## [0.4.0] - 2026-06-23

### Added

- Added accumulated segment timing counts, TTFT attribute marking, and FastAPI static request attributes.


## [0.3.0] - 2026-06-23

### Added

- Add model-agnostic AI harness instructions and canonical engineering skills.

### Fixed

- Preserve internal dispatch segment timings on the parent worker operation log.


## [0.2.1] - 2026-06-22

### Changed

- Document the release workflow in the README.


## [0.2.0] - 2026-06-22

### Added

- Add pull request and push CI/CD workflows with changelog, lint, coverage, versioning, tagging, and PyPI publishing gates.


# Changelog

