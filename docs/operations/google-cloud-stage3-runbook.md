# Google Cloud Stage 3 Observability Runbook

This runbook documents the fixed Google Cloud Logging destination for
PolicyEngine observability records.

The public runbook intentionally uses placeholders for live project IDs,
project numbers, service accounts, workload identity provider paths, and sink
writer identities. Keep concrete values in private infrastructure state,
repository/environment secrets, or internal operations documentation.

## Central Destination

- Project: `<observability-project-id>`
- Project number: `<observability-project-number>`
- Log bucket: `<observability-log-bucket>`
- Bucket location: `<bucket-location>`
- Bucket retention: 30 days
- Log Analytics: enabled
- Primary log name: `<observability-log-name>`

Required APIs:

```bash
gcloud services enable \
  logging.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  --project=<observability-project-id>
```

Direct app writes to `<observability-project-id>` are routed into the custom
observability bucket with a project-level sink:

```text
sink: <observability-app-log-sink-name>
destination: logging.googleapis.com/projects/<observability-project-id>/locations/<bucket-location>/buckets/<observability-log-bucket>
filter: LOG_ID("<observability-log-name>")
```

This sink is needed when deployed services write directly to
`projects/<observability-project-id>/logs/<observability-log-name>`. Without
it, direct writes can remain in the destination project's `_Default` bucket
instead of the analytics-enabled observability bucket.

## Cloud Run Logging

Cloud Run has two log paths.

App observability records are written directly by the observability package to
the central project. Cloud Run runtime service accounts need:

```text
roles/logging.logWriter
```

on `<observability-project-id>`.

Service accounts should be granted explicitly per deployed service and
environment:

```text
<cloud-run-gateway-service-account>
<cloud-run-worker-service-account>
```

Native Cloud Run logs are routed with a Cloud Logging sink from
the source application project:

```text
sink: <cloud-run-log-sink-name>
destination: logging.googleapis.com/projects/<observability-project-id>/locations/<bucket-location>/buckets/<observability-log-bucket>
filter: resource.type="cloud_run_revision"
writer: <sink-writer-identity>
```

The sink writer has `roles/logging.bucketWriter` on
`<observability-log-bucket>`.

## Modal Logging

Modal platform logs stay in Modal for now. Modal simulation container
observability records are written directly to Google Cloud Logging by the
observability package.

The Modal writer service account is an environment-specific service account:

```text
<modal-observability-writer-service-account>
```

It has `roles/logging.logWriter` on `<observability-project-id>`.

Modal Workload Identity Federation:

```text
provider: projects/<observability-project-number>/locations/global/workloadIdentityPools/<modal-pool-id>/providers/<modal-provider-id>
issuer: https://oidc.modal.com
audience: oidc.modal.com
```

Use a short, stable Modal claim for the Google subject mapping:

```text
google.subject=assertion.app_name
attribute.app_name=assertion.app_name
attribute.environment_name=assertion.environment_name
attribute.function_name=assertion.function_name
attribute.workspace_id=assertion.workspace_id
```

Do not map `google.subject` to a long assertion claim. Google rejects mapped
subjects longer than 127 bytes, and Modal's `sub` claim can exceed that limit.

The writer service account grants `roles/iam.workloadIdentityUser` to:

```text
principalSet://iam.googleapis.com/projects/<observability-project-number>/locations/global/workloadIdentityPools/<modal-pool-id>/<restricted-principal-selector>
```

The impersonation grant must be constrained by Modal OIDC claims or by an
equivalent provider condition. Do not use an unconstrained wildcard grant. The
condition should allow only the expected Modal app names and environments for
the services being deployed.

## Runtime Configuration

Use these variables for deployed Cloud Run and Modal environments:

```bash
OBSERVABILITY_LOG_DESTINATIONS=google_cloud_logging
OBSERVABILITY_GOOGLE_CLOUD_PROJECT=<observability-project-id>
OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME=<observability-log-name>
OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER=projects/<observability-project-number>/locations/global/workloadIdentityPools/<modal-pool-id>/providers/<modal-provider-id>
OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL=<modal-observability-writer-service-account>
```

Cloud Run does not need the Modal WIF variables. Modal does.

Do not use `OBSERVABILITY_GOOGLE_LOG_NAME`; the package reads
`OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME`.

## Smoke Tests

Write a manual structured log:

```bash
gcloud logging write <observability-log-name> \
  '{"event":"stage3_smoke_test","service_name":"<smoke-test-service-name>","schema_version":"policyengine.observability.smoke.v1"}' \
  --payload-type=json \
  --project=<observability-project-id> \
  --severity=INFO
```

Read it back:

```bash
gcloud logging read \
  'logName="projects/<observability-project-id>/logs/<observability-log-name>" AND jsonPayload.event="stage3_smoke_test"' \
  --project=<observability-project-id> \
  --limit=1 \
  --format=json
```

Find app observability logs:

```text
logName="projects/<observability-project-id>/logs/<observability-log-name>"
jsonPayload.service_name="<service-name>"
```

Find Cloud Run app observability logs:

```text
jsonPayload.service_name="<service-name>"
jsonPayload.platform="google_cloud_run"
```

Find Modal simulation container observability logs:

```text
jsonPayload.service_name="<service-name>"
jsonPayload.platform="modal"
jsonPayload.service_role="modal_worker"
```

Find native Cloud Run logs routed through the sink:

```text
resource.type="cloud_run_revision"
```

Find observability failures:

```text
jsonPayload.event="observability_internal_error"
```

## Rollback

For a failing runtime, set:

```bash
OBSERVABILITY_LOG_DESTINATIONS=stdout
```

To restore the temporary bridge destination, set:

```bash
OBSERVABILITY_GOOGLE_CLOUD_PROJECT=<temporary-bridge-project-id>
OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME=<temporary-bridge-log-name>
OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER=<temporary-bridge-modal-provider>
OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL=<temporary-bridge-writer-service-account>
```

Leave the central project and sink in place during rollback unless the sink
itself is the source of the problem.
