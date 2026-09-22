# policyengine-observability

`policyengine-observability` provides structured application logs,
OpenTelemetry traces and metrics, request propagation, and framework request
instrumentation for PolicyEngine services.

Version 2 has one explicit ownership model: `configure(config)` returns a
runtime, and every adapter or manual operation receives that runtime. The
package does not install a mutable global runtime, infer a remote destination,
or contact a remote service during configuration.

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

Service and deployment identity are always explicit. Standard OpenTelemetry
environment variables may supply OTLP transport settings.

```python
import os

from policyengine_observability import (
    DeploymentIdentity,
    LoggingConfig,
    ObservabilityConfig,
    OTelConfig,
    ServiceIdentity,
    configure,
)

central_project = os.environ["OBSERVABILITY_PROJECT_ID"]
config = ObservabilityConfig(
    service=ServiceIdentity(
        name="policyengine-api",
        namespace="policyengine.api-v1",
        version="1.2.3",
        role="api",
    ),
    deployment=DeploymentIdentity(
        environment="production",
        platform="google_cloud_run",
        region="us-central1",
    ),
    google_cloud_project_id=central_project,
    logging=LoggingConfig(shutdown_timeout_seconds=2),
    otel=OTelConfig(
        endpoint="https://COLLECTOR_HOST",
        google_audience="https://COLLECTOR_HOST",
        shutdown_timeout_seconds=3,
    ),
)
runtime = configure(config)
```

Cloud Run writes one-line JSON to standard output. It does not use the Cloud
Logging API from the application process. When no OTLP endpoint is configured,
the runtime retains local trace context for log correlation and creates no
remote exporter.

The supported OTel environment settings include:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://COLLECTOR_HOST
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_TRACES_SAMPLER_ARG=1.0
POLICYENGINE_OTEL_GOOGLE_AUDIENCE=https://COLLECTOR_HOST
```

## Flask and FastAPI

Install a framework adapter once while constructing the application. Routes
do not need observability decorators.

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

## Manual operations and spans

Use an operation for one complete non-HTTP invocation. It emits one completion
record and records duration, count, and error metrics. Use spans for child
steps; child spans do not emit additional operation completion records.

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

Function arguments and return values are never captured. Telemetry failures do
not change the function return value or replace an application exception.

## Events, application logs, and handled exceptions

The runtime accepts only configured attribute keys and scalar values.

```python
runtime.set_context(auth_result="accepted", simulation_id="sim-123")
runtime.event("simulation.dispatched", attributes={"backend": "modal"})

try:
    load_result()
except ValueError as error:
    runtime.record_exception(error, handled=True)
```

To route standard-library logs through the versioned JSON schema, instrument a
specific logger or configure root capture explicitly:

```python
import logging
from policyengine_observability import instrument_logging

logger = logging.getLogger("policyengine.api")
instrument_logging(logger, runtime)
logger.info(
    "Simulation accepted",
    extra={"policyengine_attributes": {"backend": "modal"}},
)
```

The handler ignores Google, gRPC, OTel, and this package's own loggers to avoid
recursive export failures.

## Outbound HTTP and asynchronous dispatch

Only the supplied HTTPX client is modified:

```python
import httpx
from policyengine_observability import instrument_httpx

client = httpx.AsyncClient()
instrument_httpx(client, runtime)
```

The request hook injects active W3C context and the PolicyEngine request ID.
Other clients in the process remain unchanged.

For a queued simulation, serialize the bounded correlation context with the
job request:

```python
request.observability_context = runtime.capture_context()
```

Then restore it around the worker invocation:

```python
with runtime.operation(
    "simulation.run",
    remote_context=request.observability_context,
):
    return run_simulation(request)
```

A direct continuation that starts within five minutes uses the dispatched span
as its parent. An older invocation, independent retry, or aggregate operation
starts a new trace with a link to the dispatch span.

## Modal logging and snapshots

Modal writes the same JSON immediately to standard output and may also send a
copy through one bounded background Cloud Logging writer per process:

```python
import os

from policyengine_observability import (
    GoogleCloudLoggingConfig,
    LoggingConfig,
)

central_project = os.environ["OBSERVABILITY_PROJECT_ID"]
config = ObservabilityConfig.from_env(
    service=service,
    deployment=DeploymentIdentity(
        environment="production",
        platform="modal",
    ),
    google_cloud_project_id=central_project,
    logging=LoggingConfig(
        remote=GoogleCloudLoggingConfig(
            project_id=central_project,
            log_name="policyengine-api-v1-modal",
            queue_capacity=1_000,
            batch_size=100,
            write_timeout_seconds=5,
        )
    ),
)
runtime = configure(config)
```

Remote logging starts no client until the background worker receives its first
record. The calling thread uses `put_nowait`; a full queue drops the newest
record and records a local counter.

After a Modal memory snapshot restores, rebuild process-local locks, context,
queues, threads, credentials, and exporters before accepting work:

```python
@modal.enter(snap=False)
def restore_process_state(self):
    self.runtime.restart_after_snapshot()
```

## Failure behavior and shutdown

Remote telemetry delivery is best effort. Exporters have bounded queues,
batches, retries, network deadlines, and shutdown periods. Missing credentials,
invalid configuration, unavailable DNS, denied permissions, collector failure,
queue saturation, and shutdown timeouts produce rate-limited JSON diagnostics
on standard error. Those diagnostics do not enter the failing exporter.

Call `runtime.shutdown()` during orderly process shutdown. Logging and OTel
each use the timeout configured on their respective configuration objects. A
failure in either subsystem is reported locally and does not prevent cleanup
of the other subsystem. The call is safe to repeat.

The operating policy, workload allowlist, Google Cloud deployment plan, and
rollback procedure are in
[`docs/operations/api-v1-observability.md`](docs/operations/api-v1-observability.md)
and [`deploy/gcp/README.md`](deploy/gcp/README.md).

## Release workflow

Changes include a Towncrier fragment in `changelog.d/`. Pull requests run Ruff,
tests, and coverage checks. The release workflow builds distributions and
publishes through PyPI trusted publishing.

## License

Code in this repository is released under the [MIT License](LICENSE). Original
text and figures are released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
with attribution to PolicyEngine. Third-party materials retain their terms.
