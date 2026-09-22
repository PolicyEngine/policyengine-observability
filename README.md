# policyengine-observability

`policyengine-observability` provides a shared runtime for structured logs,
OpenTelemetry traces and metrics, request propagation, and framework request
instrumentation. Each application owns its service identity, attribute policy,
logging destinations, OTLP endpoints, and credentials.

Version 2 uses an explicitly owned runtime. `configure(config)` returns that
runtime, and every adapter or manual operation receives it. Configuration does
not select a destination from the deployment platform or contact a remote
service.

## Install

Install only the integrations used by the service:

```bash
pip install "policyengine-observability[otel,otlp-grpc]"
pip install "policyengine-observability[flask,httpx,google]"
pip install "policyengine-observability[fastapi,httpx,google]"
```

The base package has no required dependencies. OpenTelemetry, OTLP exporters,
Google authentication and logging, and web frameworks are optional extras and
are imported only when configured or called.

## Configure a runtime

Service and deployment identity are explicit. Logging and OTel use separate
configuration so they can write to different stores.

```python
from policyengine_observability import (
    DeploymentIdentity,
    LoggingConfig,
    ObservabilityConfig,
    OTelConfig,
    OTLPExporterConfig,
    ServiceIdentity,
    StdoutLogDestination,
    configure,
)

config = ObservabilityConfig(
    service=ServiceIdentity(
        name="example-api",
        namespace="policyengine.example",
        version="1.2.3",
        role="api",
    ),
    deployment=DeploymentIdentity(
        environment="production",
        platform="google_cloud_run",
        region="us-central1",
    ),
    logging=LoggingConfig(
        destinations=(StdoutLogDestination(),),
    ),
    otel=OTelConfig(
        traces=OTLPExporterConfig(endpoint="collector:4317"),
        metrics=OTLPExporterConfig(endpoint="collector:4317"),
    ),
    application_attribute_keys=frozenset({"country_id", "backend"}),
    dispatch_attribute_keys=frozenset({"job_id", "run_id"}),
)
runtime = configure(config)
```

`configure` validates the complete configuration before it creates workers,
exporters, or logging handlers. Invalid values raise `ConfigurationError` with
the fields that must be corrected. Unavailable credentials or destinations
after successful validation remain nonfatal runtime failures.

The default logging destination is one-line JSON on standard output. An OTel
runtime without an exporter still creates local trace context for log
correlation. It does not send traces or metrics remotely.

## Logging destinations

Application code emits a provider-neutral record. `LoggingConfig` selects one
or more destination strategies when the runtime starts.

```python
from policyengine_observability import (
    GoogleCloudLogDestination,
    GoogleCloudLogFormatter,
)

logging = LoggingConfig(
    destinations=(
        StdoutLogDestination(
            formatter=GoogleCloudLogFormatter("trace-project"),
        ),
        GoogleCloudLogDestination(
            project_id="logging-project",
            log_name="example-api",
            queue_capacity=1_000,
            batch_size=100,
            write_timeout_seconds=5,
        ),
    ),
)
```

The formatter adds Cloud Logging trace-correlation fields only to the output
it formats. The canonical record retains the portable `trace_id`, `span_id`,
and `trace_sampled` fields.

Every queued destination has its own bounded queue and worker. A blocked or
failing destination cannot delay another destination or the application
operation that emitted the record. Queue saturation drops the newest record
and records a local diagnostic.

### Custom destinations

Use `CustomLogDestination` for a writer owned by an application or another
package:

```python
from policyengine_observability import CustomLogDestination

logging = LoggingConfig(
    destinations=(
        CustomLogDestination(
            name="internal-log-store",
            writer_factory=lambda: InternalLogWriter(),
            delivery="queued",
        ),
    ),
)
```

The writer implements `write(record)`. It may optionally implement
`write_many(records)` and `close()`. A separate integration package can also
provide a class implementing `LogDestinationStrategy`. Network writers should
always use `delivery="queued"`. Cleanup for inline writers runs on daemon
threads, and orderly shutdown waits for it only within the configured logging
shutdown timeout.

## OTLP destinations and authentication

Traces and metrics have independent exporter configurations:

```python
otel = OTelConfig(
    traces=OTLPExporterConfig(
        endpoint="https://trace-collector.example",
        protocol="http/protobuf",
        headers=(("x-api-key", "trace-key"),),
    ),
    metrics=OTLPExporterConfig(
        endpoint="metrics-collector.example:4317",
        protocol="grpc",
        headers=(("x-api-key", "metric-key"),),
    ),
)
```

For a Google ID-token protected collector, select the authentication strategy
explicitly:

```python
from policyengine_observability import GoogleIdTokenAuth

traces = OTLPExporterConfig(
    endpoint="collector.example:443",
    auth=GoogleIdTokenAuth("https://collector.example"),
)
```

`ObservabilityConfig.from_env` reads the standard common and signal-specific
OTel settings, including:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=collector.example:4317
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=trace-collector.example:4317
OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=metric-collector.example:4317
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_EXPORTER_OTLP_TRACES_HEADERS=x-api-key=trace-key
OTEL_EXPORTER_OTLP_METRICS_HEADERS=x-api-key=metric-key
```

`POLICYENGINE_OTEL_GOOGLE_AUDIENCE` selects `GoogleIdTokenAuth` for both
signals. `POLICYENGINE_OTEL_TRACES_GOOGLE_AUDIENCE` and
`POLICYENGINE_OTEL_METRICS_GOOGLE_AUDIENCE` override it per signal.

Google authentication constructed from `GCP_CREDENTIALS_JSON`,
`MODAL_IDENTITY_TOKEN`, or `OBSERVABILITY_GOOGLE_OIDC_TOKEN` stays in process
memory. The package passes credential data and subject tokens directly to
Google Auth and does not create temporary credential files. A path explicitly
provided through `GOOGLE_APPLICATION_CREDENTIALS` remains supported.

Applications can share a collector by configuring the same endpoint and
credentials. Another application can use a separate collector or any
OTLP-compatible service without changing this package.

## Flask and FastAPI

Install a framework adapter once while constructing the application:

```python
from flask import Flask
from policyengine_observability import instrument_flask

app = Flask(__name__)
instrument_flask(app, runtime)
```

```python
from fastapi import FastAPI
from policyengine_observability import instrument_fastapi

app = FastAPI()
instrument_fastapi(app, runtime)
```

Both adapters extract W3C trace context and
`X-PolicyEngine-Request-Id`, create one server span, emit one completion
record, record request metrics, and clear context-local state. Repeated calls
reuse the first runtime associated with the application.

## Application instrumentation

The package instruments framework and transport mechanics. Applications own
the names and boundaries of their domain operations.

```python
with runtime.operation(
    "simulation.run",
    attributes={"backend": "modal"},
):
    with runtime.span("simulation.build"):
        simulation = build_simulation()
    result = calculate(simulation)
```

Operations and spans support synchronous and asynchronous context management
and decoration:

```python
@runtime.span("simulation.calculate")
async def calculate(simulation):
    return await simulation.calculate()
```

Function arguments and return values are never captured. The runtime accepts
only application-configured attribute keys and scalar values.

```python
runtime.set_context(auth_result="accepted", simulation_id="sim-123")
runtime.event("simulation.dispatched", attributes={"backend": "modal"})

try:
    load_result()
except ValueError as error:
    runtime.record_exception(error, handled=True)
```

## Python logging and HTTP propagation

Instrument a specific Python logger or configure root capture explicitly:

```python
import logging
from policyengine_observability import instrument_logging

logger = logging.getLogger("policyengine.example")
instrument_logging(logger, runtime)
```

Only the supplied HTTPX client is modified:

```python
import httpx
from policyengine_observability import instrument_httpx

client = httpx.AsyncClient()
instrument_httpx(client, runtime)
```

The request hook injects active W3C context and the PolicyEngine request ID.
Other clients in the process remain unchanged.

For asynchronous dispatch, serialize the bounded correlation context with the
job request and restore it around the worker operation:

```python
request.observability_context = runtime.capture_context()

with runtime.operation(
    "simulation.run",
    remote_context=request.observability_context,
):
    return run_simulation(request)
```

## Process restoration and shutdown

After a process image or memory snapshot is restored, rebuild process-local
locks, context, queues, threads, credentials, and exporters before accepting
work:

```python
@modal.enter(snap=False)
def restore_process_state(self):
    self.runtime.restart_after_snapshot()
```

Call `runtime.shutdown()` during orderly process shutdown. Logging and OTel
each use their own timeout. After configuration validation succeeds, missing
optional dependencies, credential failures, unavailable destinations, queue
saturation, exporter errors, and shutdown timeouts produce rate-limited
diagnostics on standard error. These runtime failures do not change application
responses, return values, or exceptions.

## Release workflow

Changes include a Towncrier fragment in `changelog.d/`. Pull requests run Ruff,
tests, type checks, and coverage checks. The release workflow builds
distributions and publishes through PyPI trusted publishing.

## License

Code in this repository is released under the [MIT License](LICENSE). Original
text and figures are released under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) with attribution to
PolicyEngine. Third-party materials retain their terms.
