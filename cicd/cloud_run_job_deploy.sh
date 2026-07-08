#!/usr/bin/env bash
# ==============================================================================
# Deploy RAG Ingestion Pipeline as a GCP Cloud Run Job
# ==============================================================================
# This script builds the ingestion container using Google Cloud Build,
# deploys it as an on-demand Cloud Run Job, and sets up a Cloud Scheduler
# trigger to run it on a nightly cron schedule.
#
# Prerequisite: Authenticated via gcloud SDK with permissions to build & deploy.
# ==============================================================================

set -euo pipefail

# 1. Configuration (Customize these or set via environment)
PROJECT_ID=$(gcloud config get-value project)
LOCATION="${VERTEX_LOCATION:-us-central1}"
JOB_NAME="rag-knowledge-ingest-job"
SCHEDULER_TRIGGER_NAME="rag-ingest-daily-trigger"
IMAGE_URI="gcr.io/${PROJECT_ID}/${JOB_NAME}:latest"
SERVICE_ACCOUNT_NAME="rag-ingestion-executor"
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Bucket name (from env or fallback)
RAG_GCS_BUCKET_NAME="${RAG_GCS_BUCKET_NAME:-confluence-sharepoint-rag-bucket}"

echo "======================================================================"
echo "DEPLOYING RAG INGESTION PIPELINE TO GCP CLOUD RUN JOBS"
echo "======================================================================"
echo "Project ID:      ${PROJECT_ID}"
echo "Region:          ${LOCATION}"
echo "Job Name:        ${JOB_NAME}"
echo "Image URI:       ${IMAGE_URI}"
echo "Service Account: ${SERVICE_ACCOUNT_EMAIL}"
echo "======================================================================"

# 2. Enable Required APIs
echo "-> Enabling Google Cloud APIs..."
gcloud services enable \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    run.googleapis.com \
    cloudscheduler.googleapis.com \
    secretmanager.googleapis.com

# 3. Create Custom IAM Service Account
echo "-> Checking IAM Service Account..."
if ! gcloud iam service-accounts describe "${SERVICE_ACCOUNT_EMAIL}" &>/dev/null; then
    echo "   Creating service account '${SERVICE_ACCOUNT_NAME}'..."
    gcloud iam service-accounts create "${SERVICE_ACCOUNT_NAME}" \
        --description="Service account for executing nightly SharePoint and Confluence RAG ingestion jobs." \
        --display-name="RAG Ingestion Executor"
fi

# Assign necessary roles (GCS Storage Admin + DiscoveryEngine Editor)
echo "   Assigning roles to Service Account..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SERVICE_ACCOUNT_EMAIL}" \
    --role="roles/storage.admin" --quiet >/dev/null

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SERVICE_ACCOUNT_EMAIL}" \
    --role="roles/discoveryengine.admin" --quiet >/dev/null

# 4. Build Container using Cloud Build
echo "-> Building Docker image using Cloud Build..."
# Navigate back to rag_agent root to have correct Docker context
cd "$(dirname "$0")/.."
gcloud builds submit --tag "${IMAGE_URI}" --file cicd/Dockerfile .

# 5. Create / Update Cloud Run Job
# Best practice is to load sensitive credentials like SHAREPOINT_CLIENT_SECRET or
# CONFLUENCE_API_TOKEN from GCP Secret Manager instead of hardcoding them!
echo "-> Deploying Cloud Run Job..."
if gcloud run jobs describe "${JOB_NAME}" --region "${LOCATION}" &>/dev/null; then
    echo "   Job exists. Updating configuration..."
    gcloud run jobs update "${JOB_NAME}" \
        --image "${IMAGE_URI}" \
        --region "${LOCATION}" \
        --service-account "${SERVICE_ACCOUNT_EMAIL}" \
        --max-retries 1 \
        --task-timeout "30m" \
        --set-env-vars="RAG_GCS_BUCKET_NAME=${RAG_GCS_BUCKET_NAME},VERTEX_PROJECT_ID=${PROJECT_ID},VERTEX_LOCATION=${LOCATION},SHAREPOINT_INGEST_CONCURRENCY=5,SHAREPOINT_INGEST_MODE=graph_api"
else
    echo "   Creating new Cloud Run Job..."
    gcloud run jobs create "${JOB_NAME}" \
        --image "${IMAGE_URI}" \
        --region "${LOCATION}" \
        --service-account "${SERVICE_ACCOUNT_EMAIL}" \
        --max-retries 1 \
        --task-timeout "30m" \
        --set-env-vars="RAG_GCS_BUCKET_NAME=${RAG_GCS_BUCKET_NAME},VERTEX_PROJECT_ID=${PROJECT_ID},VERTEX_LOCATION=${LOCATION},SHAREPOINT_INGEST_CONCURRENCY=5,SHAREPOINT_INGEST_MODE=graph_api"
fi

# 6. Set up Cloud Scheduler (Nightly Trigger)
echo "-> Configuring Cloud Scheduler daily trigger..."
if gcloud scheduler jobs describe "${SCHEDULER_TRIGGER_NAME}" --location "${LOCATION}" &>/dev/null; then
    echo "   Scheduler job exists. Updating..."
    gcloud scheduler jobs update http "${SCHEDULER_TRIGGER_NAME}" \
        --location "${LOCATION}" \
        --schedule="0 2 * * *" \
        --uri="https://${LOCATION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" \
        --http-method=POST \
        --oauth-service-account-email="${SERVICE_ACCOUNT_EMAIL}"
else
    echo "   Creating new Scheduler trigger (Nightly at 2:00 AM)..."
    gcloud scheduler jobs create http "${SCHEDULER_TRIGGER_NAME}" \
        --location "${LOCATION}" \
        --schedule="0 2 * * *" \
        --uri="https://${LOCATION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" \
        --http-method=POST \
        --oauth-service-account-email="${SERVICE_ACCOUNT_EMAIL}"
fi

echo "======================================================================"
echo "🎉 DEPLOYMENT COMPLETE!"
echo "To trigger the ingestion job manually now, run:"
echo "gcloud run jobs execute ${JOB_NAME} --region=${LOCATION}"
echo "======================================================================"
