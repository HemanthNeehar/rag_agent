#!/bin/bash
# deploy_cloud_run_job.sh
set -e

# Unset temporary Cloud SDK config to use authenticated user credentials
unset CLOUDSDK_CONFIG
unset __TMP_CLOUDSDK_CONFIG


# Load project ID from active gcloud config if not provided
PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
if [ -z "$PROJECT_ID" ]; then
  echo "Error: Could not resolve GCP Project ID. Please run 'gcloud config set project YOUR_PROJECT_ID' first."
  exit 1
fi

REGION="us-central1"
REPOSITORY="rag-ingest-repo"
IMAGE_NAME="rag-ingest-job"
IMAGE_PATH="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE_NAME}:latest"

echo "=========================================================="
echo "deploy_cloud_run_job.sh - GCP CLOUD RUN JOB DEPLOYER"
echo "Project ID: ${PROJECT_ID}"
echo "Region:     ${REGION}"
echo "Target:     ${IMAGE_PATH}"
echo "=========================================================="

# 1. Enable required APIs
echo "-> Enabling Artifact Registry, Cloud Run, and Cloud Build APIs..."
gcloud services enable artifactregistry.googleapis.com run.googleapis.com cloudbuild.googleapis.com

# 2. Create Artifact Registry Repository if not exists
echo "-> Checking Artifact Registry Repository..."
gcloud artifacts repositories create "$REPOSITORY" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Docker repository for RAG Ingestion Jobs" \
    --quiet || echo "   Repository already exists."

# 3. Build Docker image using Cloud Build (performs heavy compilation in the cloud!)
echo "-> Submitting build job to Google Cloud Build..."
# Move to the rag_agent directory to isolate the build context
cd "$(dirname "$0")"
trap 'rm -f ./env.yaml' EXIT INT TERM
gcloud builds submit --tag "$IMAGE_PATH" .


# 4. Parse env variables from parent .env to env.yaml
echo "-> Parsing environment variables from .env to env.yaml..."
python3 -c '
import os
if os.path.exists("../.env"):
    with open("../.env", "r", encoding="utf-8") as f_in, open("env.yaml", "w", encoding="utf-8") as f_out:
        for line in f_in:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip("\"").strip(chr(39))
                v_escaped = v.replace("\\", "\\\\").replace("\"", "\\\"")
                f_out.write(f"{k}: \"{v_escaped}\"\n")
'


# 5. Create / Update Cloud Run Jobs
# We create 3 distinct Cloud Run Jobs sharing the same container image but with different argument parameters:
# - sharepoint-ingest-job (loads 8GB RAM, 2 vCPUs)
# - confluence-ingest-job (loads 8GB RAM, 2 vCPUs)
# - rag-engine-push-job   (loads 4GB RAM, 1 vCPU)

echo "-> Creating/Updating Cloud Run Jobs with Direct VPC egress..."

NETWORK="project-gebu-demo-sandbox-spoke-vpc"
SUBNET="project-gebu-demo-sandbox-spoke-vpc"

# Prepare env-vars-file argument if env.yaml exists
ENV_VARS_ARG=""
if [ -f "env.yaml" ]; then
  ENV_VARS_ARG="--env-vars-file=env.yaml"
fi

# --- Job A: SharePoint Ingestion ---
echo "   [1/3] Deploying 'sharepoint-ingest-job'..."
gcloud run jobs deploy sharepoint-ingest-job \
  --image "$IMAGE_PATH" \
  --args="sharepoint" \
  --memory=8Gi \
  --cpu=2 \
  --max-retries=0 \
  --task-timeout=3600s \
  --region="$REGION" \
  --network="$NETWORK" \
  --subnet="$SUBNET" \
  --vpc-egress=all-traffic \
  $ENV_VARS_ARG \
  --quiet

# --- Job B: Confluence Ingestion ---
echo "   [2/3] Deploying 'confluence-ingest-job'..."
gcloud run jobs deploy confluence-ingest-job \
  --image "$IMAGE_PATH" \
  --args="confluence" \
  --memory=8Gi \
  --cpu=2 \
  --max-retries=0 \
  --task-timeout=3600s \
  --region="$REGION" \
  --network="$NETWORK" \
  --subnet="$SUBNET" \
  --vpc-egress=all-traffic \
  $ENV_VARS_ARG \
  --quiet

# --- Job C: Vertex RAG Engine Push ---
echo "   [3/3] Deploying 'rag-engine-push-job'..."
gcloud run jobs deploy rag-engine-push-job \
  --image "$IMAGE_PATH" \
  --args="rag" \
  --memory=4Gi \
  --cpu=1 \
  --max-retries=0 \
  --task-timeout=3600s \
  --region="$REGION" \
  --network="$NETWORK" \
  --subnet="$SUBNET" \
  --vpc-egress=all-traffic \
  $ENV_VARS_ARG \
  --quiet

trap - EXIT INT TERM
rm -f ./env.yaml

echo "=========================================================="
echo "DEPLOYMENT COMPLETE! ALL 3 CLOUD RUN JOBS ARE READY!"
echo "=========================================================="
echo "To execute a job, run:"
echo "  gcloud run jobs execute sharepoint-ingest-job --region=$REGION"
echo "  gcloud run jobs execute confluence-ingest-job --region=$REGION"
echo "  gcloud run jobs execute rag-engine-push-job --region=$REGION"
echo "=========================================================="

