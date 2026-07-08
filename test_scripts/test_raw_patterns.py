import os
import json
import vertexai
from pathlib import Path
from dotenv import load_dotenv
from vertexai.preview.reasoning_engines import ReasoningEngine
from google.cloud.aiplatform_v1beta1.types import StreamQueryReasoningEngineRequest

ROOT = Path("/home/hemanth_gadavajhala/demo_agents")
load_dotenv(ROOT / ".env")

_sa = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", "")
if _sa:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(ROOT / _sa)
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "gebu-demo-sandbox")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
RESOURCE_NAME = os.getenv("RAG_AGENT_RESOURCE_NAME", "")

vertexai.init(project=PROJECT, location=LOCATION)

print(f"Loading engine: {RESOURCE_NAME}")
engine = ReasoningEngine(RESOURCE_NAME)

session_id = "test-session-raw-1"
user_id = "default-user"

print("Creating session...")
client = vertexai.Client(project=PROJECT, location=LOCATION)
remote_app = client.agent_engines.get(name=RESOURCE_NAME)
remote_app.create_session(session_id=session_id, user_id=user_id)

queries = [
    "Briefly explain Alpha architecture",
    "Show me orchestration patterns in ADK 2.0"
]

for query in queries:
    print(f"\n==================== QUERY: {query} ====================")
    request = StreamQueryReasoningEngineRequest(
        name=engine.resource_name,
        input={
            "message": query,
            "user_id": user_id,
            "session_id": session_id
        },
        class_method="stream_query"
    )

    try:
        response = engine.execution_api_client.stream_query_reasoning_engine(request=request)
        text_parts = []
        for chunk in response:
            if chunk.data:
                try:
                    data = json.loads(chunk.data)
                    parts = data.get("content", {}).get("parts", [])
                    for p in parts:
                        if "text" in p:
                            text_parts.append(p["text"])
                except Exception as e:
                    pass
        answer = "".join(text_parts)
        print(answer)
    except Exception as e:
        print(f"Error: {e}")
