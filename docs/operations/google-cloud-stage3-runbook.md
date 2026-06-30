# Google Cloud Stage 3 Observability Runbook

This runbook documents the fixed Google Cloud Logging destination for
PolicyEngine observability records.

## Central Destination

- Project: `policyengine-observability`
- Project number: `790230211054`
- Log bucket: `policyengine-observability`
- Bucket location: `global`
- Bucket retention: 30 days
- Log Analytics: enabled
- Primary log name: `policyengine-observability`

Required APIs:

```bash
gcloud services enable \
  logging.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  --project=policyengine-observability
```

## Cloud Run Logging

Cloud Run has two log paths.

App observability records are written directly by `policyengine-observability`
to the central project. Household API Cloud Run runtime service accounts need:

```text
roles/logging.logWriter
```

on `policyengine-observability`.

Initial household service accounts:

```text
household-api-gateway@policyengine-household-api.iam.gserviceaccount.com
household-api-worker@policyengine-household-api.iam.gserviceaccount.com
```

Native Cloud Run logs are routed with a Cloud Logging sink from
`policyengine-household-api`:

```text
sink: household-cloud-run-to-policyengine-observability
destination: logging.googleapis.com/projects/policyengine-observability/locations/global/buckets/policyengine-observability
filter: resource.type="cloud_run_revision"
writer: serviceAccount:service-120046258570@gcp-sa-logging.iam.gserviceaccount.com
```

The sink writer has `roles/logging.bucketWriter` on
`policyengine-observability`.

## Modal Logging

Modal platform logs stay in Modal for now. Modal simulation container
observability records are written directly to Google Cloud Logging by
`policyengine-observability`.

The Modal writer service account is:

```text
observability-writer@policyengine-observability.iam.gserviceaccount.com
```

It has `roles/logging.logWriter` on `policyengine-observability`.

Modal Workload Identity Federation:

```text
provider: projects/790230211054/locations/global/workloadIdentityPools/modal/providers/modal
issuer: https://oidc.modal.com
audience: oidc.modal.com
```

The writer service account grants `roles/iam.workloadIdentityUser` to:

```text
principalSet://iam.googleapis.com/projects/790230211054/locations/global/workloadIdentityPools/modal/*
```

The provider condition should allow household Modal app names beginning with
`policyengine-household-api`. UK Chat patterns may be included for later reuse,
but UK Chat is not required for the household API cutover.

## Runtime Configuration

Use these values for deployed household API Cloud Run and Modal environments:

```bash
OBSERVABILITY_LOG_DESTINATIONS=google_cloud_logging
OBSERVABILITY_GOOGLE_CLOUD_PROJECT=policyengine-observability
OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME=policyengine-observability
OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER=projects/790230211054/locations/global/workloadIdentityPools/modal/providers/modal
OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL=observability-writer@policyengine-observability.iam.gserviceaccount.com
```

Cloud Run does not need the Modal WIF variables. Modal does.

Do not use `OBSERVABILITY_GOOGLE_LOG_NAME`; the package reads
`OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME`.

## Smoke Tests

Write a manual structured log:

```bash
gcloud logging write policyengine-observability \
  '{"event":"stage3_smoke_test","service_name":"policyengine-observability","schema_version":"policyengine.observability.smoke.v1"}' \
  --payload-type=json \
  --project=policyengine-observability \
  --severity=INFO
```

Read it back:

```bash
gcloud logging read \
  'logName="projects/policyengine-observability/logs/policyengine-observability" AND jsonPayload.event="stage3_smoke_test"' \
  --project=policyengine-observability \
  --limit=1 \
  --format=json
```

Find household API app observability logs:

```text
logName="projects/policyengine-observability/logs/policyengine-observability"
jsonPayload.service_name="policyengine-household-api"
```

Find Cloud Run app observability logs:

```text
jsonPayload.service_name="policyengine-household-api"
jsonPayload.platform="google_cloud_run"
```

Find Modal simulation container observability logs:

```text
jsonPayload.service_name="policyengine-household-api"
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
OBSERVABILITY_GOOGLE_CLOUD_PROJECT=policyengine-api
OBSERVABILITY_GOOGLE_CLOUD_LOG_NAME=policyengine-observability
OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER=projects/389282473430/locations/global/workloadIdentityPools/modal/providers/modal
OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL=observability-writer@policyengine-api.iam.gserviceaccount.com
```

Leave the central project and sink in place during rollback unless the sink
itself is the source of the problem.
