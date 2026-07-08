#!/bin/bash
set -e

JOB_NAME=$1

echo "=========================================================="
echo "Starting Cloud Run Job: ${JOB_NAME}"
echo "Current Time: $(date)"
echo "=========================================================="

if [ -z "$JOB_NAME" ]; then
  echo "Error: No job name specified. Please provide 'sharepoint', 'confluence', or 'rag'."
  exit 1
fi

case "$JOB_NAME" in
  sharepoint)
    echo "Running SharePoint Ingestion Script..."
    python3 -u /app/rag_agent/ingest_sharepoint_gcs.py
    ;;
  confluence)
    echo "Running Confluence Ingestion Script..."
    python3 -u /app/rag_agent/ingest_gcs.py
    ;;
  rag)
    echo "Running RAG Engine Push Script..."
    python3 -u /app/rag_agent/push_rag_engine.py
    ;;
  *)
    echo "Error: Unknown job name '${JOB_NAME}'. Valid options are 'sharepoint', 'confluence', or 'rag'."
    exit 1
    ;;
esac

echo "=========================================================="
echo "Cloud Run Job Completed Successfully!"
echo "=========================================================="
