## [3.0.0] - 2026-09-23

### Breaking changes

- Replace the implicit global runtime and version 1 decorators with an explicit
  runtime returned by `configure`. Version 2 adds runtime-bound operations and
  spans, idempotent Flask and FastAPI adapters, explicit HTTPX and Python logging
  instrumentation, provider-neutral logging destination strategies, independent
  trace and metric OTLP exporters, pluggable exporter authentication, and
  allowlisted cross-service correlation. Every remote logging destination uses
  an independent bounded queue, and logging and OTel failures remain isolated
  from application responses and calculations. Consumers must replace removed
  version 1 imports and provide their own identity, destinations, attribute
  policy, endpoints, and credentials before upgrading.
  `configure` validates these values before runtime setup and raises
  `ConfigurationError` for invalid configuration.
  Inline writer cleanup now runs outside application execution and respects the
  configured logging shutdown timeout. Google credential JSON and workload
  identity subject tokens are passed to Google Auth in memory instead of being
  written to temporary files.
  Configured sensitive values are redacted from allowlisted string attributes,
  including values sent to logs, spans, and metrics. Each logging destination and
  formatter now receives a deep copy so nested mutations cannot affect another
  destination or the canonical record.
  Invalid `sensitive_values` entries now fail configuration validation before
  runtime setup. Flask callback installation failures restore the prior
  application state and do not propagate into application execution.
  Configured sensitive values are also redacted from OpenTelemetry exception
  events and local diagnostics. Attribute allowlists now require frozensets of
  non-empty strings. Flask instrumentation starts before existing request
  callbacks and completes during request teardown so failures retain exception
  details. HTTP 5xx responses set span error status. Signal-specific OTLP/HTTP
  environment endpoints are passed through unchanged.


## [1.4.1] - 2026-09-17

### Changed

- Organize the observability runtime by request, operation, segment, logging, metrics, and tracing responsibilities without changing its public API.


## [1.4.0] - 2026-07-09

### Added

- Added log profiles (`OBSERVABILITY_LOG_PROFILE`): one env var expands to a full routing configuration — `gcp-agent` (agent-native google-format stdout only, for platforms whose agent ingests stdout), `gcp-direct` (plain stdout plus queued direct Cloud Logging writes, for platforms without an agent), `plain-sync` (plain stdout, guaranteed zero threads — the kill switch), and `auto` (default: detects the platform via `OBSERVABILITY_PLATFORM`, `K_SERVICE`, or Modal markers, and preserves caller defaults when none match). Explicit `OBSERVABILITY_LOG_DESTINATIONS`/`OBSERVABILITY_STDOUT_FORMAT` still override the expansion; `gcp-direct` downgrades to `plain-sync` with a warning when no Google Cloud project is resolvable.
- Added `restart_observability()`: closes and rebuilds log destinations from configuration, for runtimes whose processes fork or restore from memory snapshots (threads and network clients survive neither). Documented for single-threaded lifecycle moments only — post-snapshot-restore hooks, post-fork hooks, before serving traffic. `restart_observability()` is a no-op when observability is disabled, so the kill switch holds across forks and restores.
- Added an agent-native stdout format (`OBSERVABILITY_STDOUT_FORMAT=google`): JSON lines carry the special keys the Cloud Run/GKE logging agent promotes to first-class LogEntry fields (severity, trace, span, labels), enabling full-fidelity Cloud Logging ingestion from stdout with no in-process network emission. Added a destination strategy registry and a generic queued transport: destinations register as `inline` (synchronous, e.g. stdout) or `remote`, and every remote destination is wrapped in a bounded queue drained by a stdlib QueueListener thread, so no remote write ever runs on a request thread. The queue drops-and-counts when full (throttled reports), and close is deadline-bounded (`OBSERVABILITY_LOG_QUEUE_MAXSIZE`, `OBSERVABILITY_LOG_QUEUE_CLOSE_TIMEOUT_SECONDS`). `register_destination` and `register_stdout_formatter` are exported at the package top level so external backends can register strategies (with `required_config` declaring the config fields a strategy needs, checked generically during profile expansion), and the Google credential helpers moved to `policyengine_observability.destinations.google_credentials`.

### Changed

- Runtime shutdown now closes log destinations first, inside the same bounded shutdown budget that bounds the OpenTelemetry flush; one deadline is shared across all destination closes. The Google Cloud Logging client library's one-time instrumentation diagnostic entry is suppressed, and unknown `OBSERVABILITY_STDOUT_FORMAT` names are reported through the internal-error channel instead of silently falling back to plain, and a registered stdout-formatter factory that raises degrades to the built-in plain formatter (with a report) instead of breaking configuration.
- Google Cloud Logging writes now carry an explicit per-call budget (default 10 seconds, `OBSERVABILITY_GOOGLE_WRITE_TIMEOUT_SECONDS`, clamped to sane bounds) that caps both the call and its transient-error retries, replacing the transport defaults that could hold a single write for up to 60 seconds when the Logging API degrades.

### Removed

- Removed the `policyengine_observability.google_credentials` module path; the credential helpers live at `policyengine_observability.destinations.google_credentials` and remain re-exported from the package top level (`load_google_credentials`, `configure_google_application_credentials`), which no known consumer's imports go beyond.


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

