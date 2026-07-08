FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Google Cloud SDK (gcloud CLI)
RUN echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list \
    && curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && apt-get update -y && apt-get install google-cloud-cli -y \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency files into a subfolder `rag_agent` so they mirror the package structure
COPY requirements.txt requirements_adk.txt ./rag_agent/

# Install Python requirements (combining both standard and ADK/parser requirements)
RUN pip install --no-cache-dir -r ./rag_agent/requirements.txt \
    && pip install --no-cache-dir -r ./rag_agent/requirements_adk.txt

# Copy application source code (exclusively the rag_agent files) into /app/rag_agent
COPY . ./rag_agent/

# Ensure run_job.sh is executable
RUN chmod +x ./rag_agent/run_job.sh

# Set the Python path to ensure module imports like `rag_agent.xxx` work perfectly
ENV PYTHONPATH=/app

# Define the entrypoint
ENTRYPOINT ["/app/rag_agent/run_job.sh"]
