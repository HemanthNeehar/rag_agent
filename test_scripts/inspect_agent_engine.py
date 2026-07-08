import os
import asyncio
import vertexai
from pathlib import Path
from dotenv import load_dotenv

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

print(f"Initializing vertexai.Client with project={PROJECT}, location={LOCATION}")
client = vertexai.Client(project=PROJECT, location=LOCATION)

print(f"Getting AgentEngine: {RESOURCE_NAME}")
remote_app = client.agent_engines.get(name=RESOURCE_NAME)

print("\n--- Listing remote_app attributes/methods ---")
print("Class:", type(remote_app))
for attr in dir(remote_app):
    if not attr.startswith("_"):
        try:
            val = getattr(remote_app, attr)
            print(f"  {attr}: {type(val)}")
        except Exception as e:
            print(f"  {attr}: (error reading: {e})")

print("\nTrying to query synchronously if possible...")
# Let's see if there is any predict/query/execute method or call method
if hasattr(remote_app, "query"):
    print("Found query method! Calling remote_app.query...")
    try:
        res = remote_app.query(message="Explain Google Next Announcements Overview")
        print("Success:", res)
    except Exception as e:
        print("Failed remote_app.query:", e)
else:
    print("No query method found on remote_app.")

async def test_async_stream():
    if hasattr(remote_app, "async_stream_query"):
        print("\nFound async_stream_query! Testing async stream...")
        try:
            async for event in remote_app.async_stream_query(
                message="Explain Google Next Announcements Overview",
                user_id="test-user",
                session_id="test-session"
            ):
                print("Event:", event)
        except Exception as e:
            print("Failed async_stream_query:", e)
    elif hasattr(remote_app, "stream_query"):
        print("\nFound stream_query! Testing stream...")
        try:
            for event in remote_app.stream_query(
                message="Explain Google Next Announcements Overview",
                user_id="test-user",
                session_id="test-session"
            ):
                print("Event:", event)
        except Exception as e:
            print("Failed stream_query:", e)
    else:
        print("\nNo streaming methods found on remote_app.")

asyncio.run(test_async_stream())
