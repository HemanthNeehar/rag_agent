"""Deploy the RAG agent to Vertex AI Agent Engine as a standard **AdkApp**.

Unlike the A2A deploy (deploy.py), this wraps the agent in AdkApp which is
simpler to test — the GCP Console Playground can invoke it directly via the
auto-generated `query` method without needing an A2A client.

Usage:
    python -m rag_agent.deploy_adk
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_deploy_dir = Path(__file__).resolve().parent
_repo_root = _deploy_dir.parent

load_dotenv(_repo_root / ".env")
load_dotenv(_deploy_dir / ".env", override=True)

import vertexai
from vertexai._genai import agent_engines as ae_module

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "gebu-demo-sandbox").strip()
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1").strip()
STAGING_BUCKET = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc")
DISPLAY_NAME = "Corporate RAG Agent (ADK)"

CREDENTIALS_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
if CREDENTIALS_FILE and os.path.isfile(CREDENTIALS_FILE):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = CREDENTIALS_FILE

print(f"[deploy_adk] Project: {PROJECT_ID} | Location: {LOCATION}")
vertexai.init(project=PROJECT_ID, location=LOCATION, staging_bucket=f"gs://{STAGING_BUCKET}")

# ── Load the agent ──────────────────────────────────────────────────────────
# Switch cwd so rag_agent package imports resolve correctly
_original_cwd = os.getcwd()
os.chdir(_repo_root)
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# agent_rag.py uses RAG corpus; agent.py uses Discovery Engine.
# RAG_CORPUS_ID presence in env selects the correct agent automatically.
if os.getenv("RAG_CORPUS_ID"):
    from rag_agent.agent_rag import root_agent
    print("[deploy_adk] Using agent_rag.py (Vertex AI RAG corpus)")
else:
    from rag_agent.agent import root_agent
    print("[deploy_adk] Using agent.py (Discovery Engine)")

# ── Build env vars to pass into the container ───────────────────────────────
_RESERVED = frozenset({
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_QUOTA_PROJECT",
    "GOOGLE_CLOUD_LOCATION", "PORT", "K_SERVICE", "K_REVISION",
    "K_CONFIGURATION", "GOOGLE_APPLICATION_CREDENTIALS",
})

_wanted_keys = (
    "GEMINI_MODEL",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "RAG_CORPUS_ID",
    "RAG_GCS_BUCKET_NAME",
    "RAG_DATA_STORE_ID",
    "RAG_DATA_STORE_LOCATION",
    "FIRESTORE_DATABASE_ID",
    "ADK_DISABLE_JSON_SCHEMA_FOR_FUNC_DECL",
)
env_vars: dict[str, str] = {
    "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
    "ADK_DISABLE_JSON_SCHEMA_FOR_FUNC_DECL": "1",
}
for k in _wanted_keys:
    v = (os.getenv(k) or "").strip()
    if v and k not in _RESERVED:
        env_vars[k] = v

# ── Read requirements ────────────────────────────────────────────────────────
# Use requirements_adk.txt (no [a2a] extra) to avoid the LlmAgent.mode error
# that occurs when the A2A executor stack is present in an AdkApp deployment.
_req_file = _deploy_dir / "requirements_adk.txt"
if not _req_file.is_file():
    _req_file = _deploy_dir / "requirements.txt"  # fallback

requirements = [
    line.strip()
    for line in _req_file.read_text().splitlines()
    if line.strip() and not line.strip().startswith("#")
]

print(f"[deploy_adk] env_vars keys: {', '.join(sorted(env_vars))}")
print(f"[deploy_adk] requirements: {len(requirements)} package(s)")
print("[deploy_adk] extra_packages: ['rag_agent']")

# ── Deploy ───────────────────────────────────────────────────────────────────
try:
    # Use the new client.agent_engines.create() API (not the old ReasoningEngine.create).
    # Only this path sends agentFramework="google-adk" to the Vertex AI API,
    # which is what makes the GCP Console show the Playground tab.
    from vertexai.preview import reasoning_engines

    adk_app = reasoning_engines.AdkApp(
        agent=root_agent,
        enable_tracing=False,
        env_vars=env_vars,
    )

    client = vertexai.Client(project=PROJECT_ID, location=LOCATION)
    remote = client.agent_engines.create(
        agent=adk_app,
        config={
            "display_name": DISPLAY_NAME,
            "agent_framework": "google-adk",   # ← activates Playground in GCP Console
            "requirements": requirements,
            "extra_packages": ["rag_agent"],
            "staging_bucket": f"gs://{STAGING_BUCKET}",
            "env_vars": env_vars,
        },
    )

    resource_name = (
        getattr(remote, "resource_name", None)
        or getattr(remote, "name", None)
        or getattr(getattr(remote, "_api_resource", None), "name", None)
    )
    if not resource_name:
        # Last resort: parse from the object repr which contains name='projects/...'
        import re as _re
        m = _re.search(r"name='(projects/[^']+)'", str(remote))
        resource_name = m.group(1) if m else "unknown (check Vertex AI console)"
    print("\n" + "=" * 60)
    print("CORPORATE RAG AGENT DEPLOYED (ADK / ReasoningEngine)")
    print("=" * 60)
    print(f"Resource name : {resource_name}")
    print(f"Display name  : {DISPLAY_NAME}")
    print("\nTest in GCP Console:")
    print(f"  Vertex AI → Agent Engine → {DISPLAY_NAME} → Test")
    print("\nOr via SDK:")
    print(f'  remote = vertexai.Client(project="{PROJECT_ID}", location="{LOCATION}").agent_engines.get(name="{resource_name}")')
    print('  remote.query(input="What are agent design patterns?")')
    print("=" * 60)
    print(f"DEPLOY_RESOURCE_NAME={resource_name}")

except Exception as e:
    print(f"[deploy_adk] ERROR: {e}", file=sys.stderr)
    raise
finally:
    os.chdir(_original_cwd)

