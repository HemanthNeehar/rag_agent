import os
import json
import vertexai
from pathlib import Path
from dotenv import load_dotenv
from vertexai.preview.reasoning_engines import ReasoningEngine

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

print("\n--- Listing available methods/attributes ---")
for attr in dir(engine):
    if not attr.startswith("_"):
        print(f"  {attr}")

print("\nQuerying ReasoningEngine via engine.query()...")
try:
    # Let's see what args the engine query method expects
    # In deploy_adk.py, the test instruction is: remote.query(input="What are agent design patterns?")
    # Or maybe it has different signature depending on the deployed agent.
    # Let's inspect the engine's query method's docstring and signature first:
    import inspect
    print("Query method signature:", inspect.signature(engine.query))
except Exception as e:
    print("Could not inspect signature:", e)

try:
    res = engine.query(input="Explain Google Next Announcements Overview")
    print("\nResult:")
    print(res)
except Exception as e:
    print("\nQuery failed with error:", e)
