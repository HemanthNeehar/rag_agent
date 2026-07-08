import os
import json
import vertexai
from pathlib import Path
from dotenv import load_dotenv
from vertexai.preview.reasoning_engines import ReasoningEngine
from google.cloud.aiplatform_v1beta1.types import StreamQueryReasoningEngineRequest

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

# Auth
_sa = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", "")
if _sa:
    _sa_path = Path(_sa) if Path(_sa).is_absolute() else ROOT / _sa
    if _sa_path.exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_sa_path)
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "gebu-demo-sandbox")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
RESOURCE_NAME = os.getenv("RAG_AGENT_RESOURCE_NAME", "")

vertexai.init(project=PROJECT, location=LOCATION)

print(f"Initializing ReasoningEngine with resource: {RESOURCE_NAME}")
engine = ReasoningEngine(RESOURCE_NAME)

request = StreamQueryReasoningEngineRequest(
    name=engine.resource_name,
    input={
        "message": "Explain Google Next Announcements Overview",
        "user_id": "test-user",
        "session_id": "test-session"
    },
    class_method="stream_query"
)

print("Sending streaming query request...")
response = engine.execution_api_client.stream_query_reasoning_engine(request=request)

for i, chunk in enumerate(response):
    print(f"\n--- Chunk {i} ---")
    print(f"data: {repr(chunk.data)}")
    print(f"type: {type(chunk)}")
    print(f"fields: {dir(chunk)}")
    # If there is an error field, print it
    for attr in dir(chunk):
        if not attr.startswith("_") and attr not in ["data", "count", "index"]:
            try:
                print(f"  {attr}: {getattr(chunk, attr)}")
            except Exception as e:
                print(f"  {attr}: (error reading: {e})")
