"""AgentCard for RAG Agent (local ``to_a2a`` + Vertex ``A2aAgent``)

Uses ``vertexai...create_agent_card`` for **HTTP+JSON** transport (Agent engine / registry).
"""

import os
from pathlib import Path

from a2a.types import AgentSkill
from dotenv import load_dotenv
from vertexai.preview.reasoning_engines.templates.a2a import create_agent_card

load_dotenv(Path(__file__).parent / ".env")

_base_url = os.getenv("RAG_AGENT_URL", "http://localhost:8015").rstrip("/") + "/"
_project = os.getenv("GOOGLE_CLOUD_PROJECT")
_location = os.getenv("GOOGLE_CLOUD_LOCATION")

if not _base_url or not _project or not _location:
    raise RuntimeError("Missing required env vars")

_project_path = f"projects/{_project}/locations/{_location}"

skills = [
    AgentSkill(
        id="query_company_knowledge",
        name="Query Company Knowledge",
        description="Searches company Confluence pages and SharePoint sites for policies, layouts, and documentation.",
        tags=["confluence", "sharepoint", "rag", "knowledge", "a2a"],
        examples=[
            "Find the Private Service Connect configuration diagram.",
            "What is the company's observability and tracing policy?",
            "What environment variables are mapped to the dev database?"
        ],
    ),
]


rag_agent_card = create_agent_card(
    agent_name="rag_agent",
    description=(
        "Retrieve company policies, configurations, and document sheets from Confluence and SharePoint."
        " Grounded RAG agent using Vertex AI Search."
    ),
    skills=skills,
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
)

rag_agent_card.url = _base_url
