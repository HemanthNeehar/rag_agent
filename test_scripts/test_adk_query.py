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

vertexai.init(project=PROJECT, location=LOCATION)
client = vertexai.Client(project=PROJECT, location=LOCATION)
remote_app = client.agent_engines.get(name=RESOURCE_NAME)

async def run_query(session_id=None, create_first=False):
    print(f"\n=== Test (session_id={session_id}, create_first={create_first}) ===")
    
    actual_session = session_id
    if create_first and session_id:
        try:
            print(f"Creating session: {session_id}...")
            # If there is a create_session method
            if hasattr(remote_app, "create_session"):
                sess = remote_app.create_session(session_id=session_id)
                print("Created session successfully:", sess)
            elif hasattr(remote_app, "async_create_session"):
                sess = await remote_app.async_create_session(session_id=session_id)
                print("Created session successfully (async):", sess)
        except Exception as e:
            print("Failed to create session:", e)
            
    try:
        print("Starting stream query...")
        kwargs = {
            "message": "Explain Google Next Announcements Overview",
            "user_id": "test-user"
        }
        if actual_session is not None:
            kwargs["session_id"] = actual_session
            
        async for event in remote_app.async_stream_query(**kwargs):
            print("Event:", event)
    except Exception as e:
        print("Query failed:", e)

async def main():
    # 1. No session_id at all
    await run_query(session_id=None)
    
    # 2. Empty session_id
    await run_query(session_id="")
    
    # 3. Create session first
    await run_query(session_id="custom-session-123", create_first=True)

asyncio.run(main())
