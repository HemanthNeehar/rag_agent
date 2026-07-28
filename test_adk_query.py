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

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "agent-ops-494011")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us")
RESOURCE_NAME = os.getenv("RAG_AGENT_RESOURCE_NAME", "")

vertexai.init(project=PROJECT, location=LOCATION)
client = vertexai.Client(project=PROJECT, location=LOCATION)
remote_app = client.agent_engines.get(name=RESOURCE_NAME)

async def run_query(message: str, user_email: str, user_groups: list, label: str):
    print(f"\n" + "="*80)
    print(f" DEMO SCENARIO: {label}")
    print(f" User Identity:  Email={user_email} | Groups={user_groups}")
    print(f" Query Message:  '{message}'")
    print("="*80)
    
    try:
        print(" -> Contacting RAG Agent Engine...")
        kwargs = {
            "message": message,
            "user_id": "test-user-id",
            "user_email": user_email,
            "user_groups": user_groups
        }
            
        print(" -> Streaming Response:")
        async for event in remote_app.async_stream_query(**kwargs):
            # 1. If it's a string, print directly
            if isinstance(event, str):
                print(event, end="", flush=True)
                continue
                
            # 2. Try dict-based access
            if isinstance(event, dict):
                content = event.get('content') or event.get('data', {}).get('content')
                if content:
                    parts = content.get('parts', [])
                    for part in parts:
                        if isinstance(part, dict) and 'text' in part:
                            print(part['text'], end="", flush=True)
                        elif hasattr(part, 'text') and part.text:
                            print(part.text, end="", flush=True)
                else:
                    if 'text' in event:
                        print(event['text'], end="", flush=True)
                    else:
                        print(f"[{event}]", end="", flush=True)
                continue
                
            # 3. Try object-based attribute access
            try:
                if hasattr(event, 'content'):
                    parts = event.content.parts
                    for part in parts:
                        if hasattr(part, 'text') and part.text:
                            print(part.text, end="", flush=True)
                elif hasattr(event, 'text'):
                    print(event.text, end="", flush=True)
                elif hasattr(event, 'data') and event.data:
                    import json
                    try:
                        data = json.loads(event.data)
                        if isinstance(data, dict):
                            parts = data.get('content', {}).get('parts', [])
                            for part in parts:
                                if isinstance(part, dict) and 'text' in part:
                                    print(part['text'], end="", flush=True)
                    except Exception:
                        print(event.data, end="", flush=True)
                else:
                    print(f"[{str(event)}]", end="", flush=True)
            except Exception:
                print(f"[{str(event)}]", end="", flush=True)
        print("\n" + "-"*80)
    except Exception as e:
        print(f"\n [Error] Query failed: {e}")

async def main():
    # TEST TARGET: GIT_Counts.xlsx or Drawing.vsdx (which are restricted under YourApplicationSite)
    target_query = "What is the content of GIT_Counts.xlsx?"
    
    # -------------------------------------------------------------------------
    # SCENARIO 1: Guest User (ACCESS DENIED)
    # -------------------------------------------------------------------------
    await run_query(
        message=target_query,
        user_email="guest@example.com",
        user_groups=[],
        label="UNAUTHORIZED ACCESS TRIAL (Guest User - Restricted Document)"
    )
    
    # Small pause between runs for clarity
    await asyncio.sleep(2)
    
    # -------------------------------------------------------------------------
    # SCENARIO 2: Authorized Site Member (ACCESS GRANTED)
    # -------------------------------------------------------------------------
    # By passing either their email "firstname.lastname@lumen.com" 
    # OR their authorized SharePoint Group "tom application site members"
    await run_query(
        message=target_query,
        user_email="firstname.lastname@lumen.com",
        user_groups=["tom application site members"],
        label="AUTHORIZED ACCESS TRIAL (Site Member Group - Access Granted)"
    )

if __name__ == "__main__":
    asyncio.run(main())
