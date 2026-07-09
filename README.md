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

## Log routing profiles

Log routing is owned by this package: consumers set one env var and the
package expands it into destinations, formats, and transport.

```bash
OBSERVABILITY_LOG_PROFILE=gcp-agent   # or gcp-direct | plain-sync | auto
```

- `gcp-agent` — google-format stdout only, for platforms whose logging
  agent ingests stdout (Cloud Run, GKE). Fully synchronous, zero
  threads; the agent ships lines to Cloud Logging with severity, trace,
  span, and labels promoted to first-class LogEntry fields.
- `gcp-direct` — plain stdout (the durable record) plus queued direct
  Cloud Logging writes, for platforms with no ingesting agent (Modal).
  Requires a resolvable Google Cloud project; downgrades to `plain-sync`
  with a warning otherwise.
- `plain-sync` — plain stdout only, guaranteed zero threads. Local
  development and the kill switch: setting it disables all background
  log machinery.
- `auto` (default) — detects the platform via `OBSERVABILITY_PLATFORM`
  (`google_cloud_run`/`modal`), then `K_SERVICE`, then Modal env
  markers; when nothing matches, caller-supplied defaults apply.

The granular controls still exist underneath and override the profile's
expansion when set explicitly:

```bash
OBSERVABILITY_LOG_DESTINATIONS=stdout,google_cloud_logging
OBSERVABILITY_STDOUT_FORMAT=google
OBSERVABILITY_GOOGLE_CLOUD_PROJECT=policyengine-api
OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME=policyengine-observability
```

Destinations are named strategies: `stdout` is `inline` (synchronous on
the caller's thread), and every `remote` strategy — `google_cloud_logging`
today; future backends register the same way — is automatically wrapped
in the queued transport below. Google Cloud Logging uses Application
Default Credentials and requires permission to create log entries,
typically through `roles/logging.logWriter`.

Backends plug in through two top-level hooks, `register_destination`
(with `transport="inline"|"remote"` and an optional `required_config`
tuple naming the config fields the strategy needs — profiles that name
the strategy downgrade gracefully when one is missing) and
`register_stdout_formatter`. Registration happens at import time, so an
external backend module must be imported before observability is
configured. Backend-specific knobs are the strategy's own: the Google
strategy reads `OBSERVABILITY_GOOGLE_WRITE_TIMEOUT_SECONDS` itself at
construction (so it is re-read on `restart_observability()`) rather
than through a core config field. Name lookups for destinations,
formatters, and profiles all forgive case, whitespace, and
hyphen/underscore variance; an unknown format name falls back to plain
and is reported through the internal-error channel, and a registered
formatter factory that raises degrades to the built-in plain formatter
the same way rather than breaking configuration.

## Log emission and delivery semantics

Remote destinations never write on a request thread. The log call only
snapshots the payload, stamps the enqueue time, and appends to a bounded
in-memory queue (microseconds, never blocks, never raises); a stdlib
`QueueListener` thread drains the queue and performs the writes, sending
the enqueue time as the entry timestamp so delayed writes keep their
event time.

Delivery through the queue is best-effort by design — stdout is the
durable sibling record. When the queue is full the newest record is
dropped, and drops are counted and reported through the internal-error
channel (first drop, then every 100th). Write failures are likewise
reported and the record dropped; there is deliberately no breaker or
retry queue in the transport. Each Google write carries an explicit
budget that caps the call and its transient-error retries:

```bash
OBSERVABILITY_GOOGLE_WRITE_TIMEOUT_SECONDS=10.0
OBSERVABILITY_LOG_QUEUE_MAXSIZE=1000
OBSERVABILITY_LOG_QUEUE_CLOSE_TIMEOUT_SECONDS=2.0
```

(The bounded write rebinds the client's private gapic method at
construction; when that handle is unavailable — HTTP transports,
injected fakes — the library's ~60s default applies, which is harmless
off the request path. All numeric knobs are clamped, so `0`, negative,
or non-finite values can never disable or unbound a mechanism. The
Google client library's one-time instrumentation diagnostic entry is
suppressed at construction, so the stream carries only the records the
service asked to write.)

Shutdown closes log destinations inside the same bounded budget that
flushes OpenTelemetry (`OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS`); a
queue that cannot drain before its deadline is abandoned with a report.
A hard kill loses whatever was still queued. Note the google stdout
format sets no `time` key: stdout emission is synchronous, so the
agent's receive time is the correct event time.

Processes that fork or restore from memory snapshots do not preserve
threads or network clients. Call `restart_observability()` from the
post-restore or post-fork hook (for example gunicorn `post_fork` when
using `--preload`, or a Modal post-snapshot hook) — it closes and
rebuilds destinations from configuration, and must only be called from
single-threaded lifecycle moments, before serving traffic. It is a
no-op when observability is disabled, so the kill switch holds across
forks and restores.

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
