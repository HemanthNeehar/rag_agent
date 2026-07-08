# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Install system dependencies (including gcloud CLI for GCS uploads)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    git \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] http://packages.cloud.google.com/apt cloud-sdk main" \
    | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list \
    && curl https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    | apt-key --keyring /usr/share/keyrings/cloud.google.gpg add - \
    && apt-get update && apt-get install -y google-cloud-cli \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir pypdf python-docx python-pptx pandas openpyxl xlrd tabulate

# Copy the application code
COPY . .

# Set environment variable defaults
ENV PYTHONUNBUFFERED=1
ENV SHAREPOINT_INGEST_MODE=graph_api
ENV SHAREPOINT_INGEST_CONCURRENCY=5

# Entrypoint script that runs ingestion and pushes to the RAG engine
CMD ["sh", "-c", "python ingest_sharepoint_gcs.py && python ingest_gcs.py && python push_rag_engine.py"]
