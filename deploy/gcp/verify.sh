#!/usr/bin/env bash
set -euo pipefail

project="policyengine-observability"
region="us-central1"
collector="policyengine-api-v1-otel-collector"

gcloud logging buckets describe policyengine-observability \
  --location=global \
  --project="${project}" \
  --format='value(name,retentionDays,analyticsEnabled)'

gcloud run services describe "${collector}" \
  --region="${region}" \
  --project="${project}" \
  --format='value(status.url,spec.template.spec.serviceAccountName)'

gcloud projects get-iam-policy "${project}" \
  --flatten='bindings[].members' \
  --filter='bindings.role:roles/telemetry.writer OR bindings.role:roles/serviceusage.serviceUsageConsumer OR bindings.role:roles/logging.logWriter' \
  --format='table(bindings.role,bindings.members)'

gcloud logging views get-iam-policy _AllLogs \
  --bucket=policyengine-observability \
  --location=global \
  --project="${project}" \
  --format=json

gcloud iam workload-identity-pools providers describe modal-api-v1 \
  --workload-identity-pool=modal-api-v1 \
  --location=global \
  --project="${project}" \
  --format='yaml(state,attributeCondition,attributeMapping,oidc)'

for source_project in policyengine-api policyengine-simulation-entry; do
  gcloud logging sinks describe api-v1-central-observability \
    --project="${source_project}" \
    --format='yaml(destination,filter,writerIdentity)'
done

gcloud logging sinks describe policyengine-observability-app-logs \
  --project="${project}" \
  --format='yaml(destination,filter)'

gcloud logging sinks describe _Default \
  --project="${project}" \
  --format='yaml(filter,exclusions)'
