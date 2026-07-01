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

