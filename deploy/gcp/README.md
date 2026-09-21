# Google Cloud deployment plan

This directory defines the centralized API v1 observability resources in the
`policyengine-observability` project. The workload boundary is defined in
[`workload-inventory.yaml`](workload-inventory.yaml). Applications absent from
that inventory receive no credentials or destination permissions.

These files are a reviewable deployment plan. Applying them changes live IAM,
Cloud Logging routing, Cloud Run, and monitoring resources and therefore
requires an operator-approved deployment window.

## Resources

| File | Resource |
| --- | --- |
| `collector/config.yaml` | OTLP gRPC receiver, bounded processors, and Google Telemetry API exporter for traces and metrics |
| `collector/Dockerfile` | Google-built OTel Collector 0.160.0 plus the reviewed configuration |
| `collector/service.yaml` | Authenticated Cloud Run collector with fixed CPU, memory, concurrency, health checks, and scaling bounds |
| `log-routing.yaml` | Exact Cloud Run source sinks, restricted Modal direct-log sink, and `_Default` duplicate exclusion |
| `iam.yaml` | Collector and Modal service accounts, Cloud Run invokers, and a dedicated Modal API v1 identity provider |
| `dashboard.json` | Initial request, latency, error, dropped-item, and exporter-failure dashboard |
| `alerts.yaml` | Initial alert policy inputs |
| `verify.sh` | Read-only resource and routing checks after deployment |

The collector accepts traces and metrics. Application logs do not enter the
collector. Cloud Run JSON output uses source-project sinks, while authorized
Modal processes use the package's bounded Cloud Logging writer.

The initial collector tail policy retains 100% of traces. Separate error and
30-second latency policies are evaluated before the general policy. If the
general percentage is reduced after the volume review, those two policies keep
error and slow traces. SDK head sampling must remain at 100% for the collector
to receive spans needed for this decision.

## Deployment order

### 1. Enable services

```bash
gcloud services enable \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  cloudresourcemanager.googleapis.com \
  iamcredentials.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  run.googleapis.com \
  sts.googleapis.com \
  telemetry.googleapis.com \
  tracing.googleapis.com \
  --project=policyengine-observability
```

### 2. Create identities

Create these service accounts in `policyengine-observability`:

```text
policyengine-otel-collector@policyengine-observability.iam.gserviceaccount.com
policyengine-api-v1-modal@policyengine-observability.iam.gserviceaccount.com
```

Grant only the roles listed in [`iam.yaml`](iam.yaml). The collector receives
`roles/telemetry.writer` and `roles/serviceusage.serviceUsageConsumer`. The Modal identity receives
`roles/logging.logWriter` and collector invocation permission. The four
existing Cloud Run identities in the inventory receive collector invocation
permission on the collector service only.

Create a separate `modal-api-v1` workload identity pool and provider using the
issuer, audience, mappings, workspace, environment, and application condition
in `iam.yaml`. Do not modify the existing `modal/modal` provider during this
deployment; it belongs to applications excluded from this change. Grant the
new provider permission to impersonate only the API v1 Modal service account.

Before enabling the provider, decode one production and one staging Modal
identity token locally and confirm that `workspace_id`, `environment_name`,
and `app_name` exactly match the reviewed condition.

### 3. Build and deploy the collector

```bash
gcloud artifacts repositories create observability \
  --repository-format=docker \
  --location=us-central1 \
  --immutable-tags \
  --project=policyengine-observability

gcloud builds submit deploy/gcp/collector \
  --tag=us-central1-docker.pkg.dev/policyengine-observability/observability/otel-collector:0.160.0-api-v1-1 \
  --project=policyengine-observability

gcloud run services replace deploy/gcp/collector/service.yaml \
  --region=us-central1 \
  --project=policyengine-observability
```

Apply `roles/run.invoker` bindings for the five identities listed in
`iam.yaml`. Do not grant unauthenticated invocation. Record the HTTPS service
URL as both `OTEL_EXPORTER_OTLP_ENDPOINT` and
`POLICYENGINE_OTEL_GOOGLE_AUDIENCE` in participating service configuration.

### 4. Configure log routing

Create one aggregated sink in each source project using the exact service and
schema filters in [`log-routing.yaml`](log-routing.yaml). Grant each generated
sink writer identity `roles/logging.bucketWriter` on the central
`policyengine-observability` bucket.

Update `policyengine-observability-app-logs` to the listed direct-log filter.
Add the listed exclusion to `_Default`; this prevents a direct Modal log from
being stored in both `_Default` and the analytics bucket. Preserve the existing
Cloud Audit Log exclusions.

After routing one synthetic record per participating service, confirm each
`insertId` exists exactly once in the central project.

### 5. Create dashboard and alerts

```bash
gcloud monitoring dashboards create \
  --config-from-file=deploy/gcp/dashboard.json \
  --project=policyengine-observability
```

Create the API-ready alert policies with:

```bash
.venv/bin/python deploy/gcp/create_alerts.py
```

The script is idempotent by policy display name. It leaves notification-channel
configuration empty when the project has no channel; add operator-owned channel
identifiers after creating the relevant email, Slack, or paging destination.

### 6. Verify before consumer deployment

```bash
bash deploy/gcp/verify.sh
```

Then use an approved workload identity to send one trace and metric. Attempt
the same request with a synthetic Modal token whose application name is not in
the inventory; token exchange or collector invocation must return permission
denial. Do not invoke an excluded application to perform this check.

For an operator-run Cloud Run identity check, temporarily grant the operator
`roles/iam.serviceAccountTokenCreator` on one inventoried runtime identity, run:

```bash
.venv/bin/python deploy/gcp/verify_otel.py \
  --endpoint=https://policyengine-api-v1-otel-collector-790230211054.us-central1.run.app \
  --service-account=sim-entry-beta-runtime@policyengine-simulation-entry.iam.gserviceaccount.com
```

Remove the temporary operator binding immediately after the check. The script
requires an authenticated `gcloud` session, sends one trace and metric, verifies
both Google Cloud stores, and confirms that the collector rejects OTLP logs.

Use [`verify_modal_wif.py`](verify_modal_wif.py) with the Modal CLI to run an
allowed app name and a synthetic denied app name. The remote function exchanges
its automatically injected Modal OIDC token, verifies service-account access,
invokes the collector, and writes one routing record without exposing any
token. Run an allowed app name from a temporary non-allowlisted environment to
verify the environment restriction, then delete that environment.

## Rollback

1. Remove the OTel endpoint from participating service configuration. Local
   structured logging continues and no remote OTel exporter is created.
2. Remove Modal remote logging configuration. Modal JSON output continues.
3. Revert each participating service to its previous package version and
   deployment revision.
4. Remove the new source sinks and restore the prior central direct-log sink
   filter and `_Default` exclusion state.
5. Remove invoker bindings, disable the `modal-api-v1` provider, and disable or
   delete the collector service.
6. Keep the central bucket during the retention period unless the stored data
   itself caused the incident.

Rollback does not modify the existing `modal/modal` provider or any excluded
application deployment.
