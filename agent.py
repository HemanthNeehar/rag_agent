import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv

from google.adk.agents import Agent
from google.adk.tools import DiscoveryEngineSearchTool, SearchResultMode

agent_dir = Path(__file__).parent
load_dotenv(agent_dir.parent / ".env")

## Load instruction from markdown file
instruction_file = agent_dir / "INSTRUCTION.md"
with open(instruction_file, "r", encoding="utf-8") as f:
    AGENT_INSTRUCTION = f.read()

## Optional : Service account path from env
service_account_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH")

if service_account_path:
    if not os.path.isabs(service_account_path):
        service_account_path = str(agent_dir / service_account_path)
    if Path(service_account_path).exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = service_account_path
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
        if os.getenv("GOOGLE_CLOUD_PROJECT"):
            os.environ["GOOGLE_CLOUD_PROJECT"] = os.getenv("GOOGLE_CLOUD_PROJECT")
        if os.getenv("GOOGLE_CLOUD_LOCATION"):
            os.environ["GOOGLE_CLOUD_LOCATION"] = os.getenv("GOOGLE_CLOUD_LOCATION")

project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "gebu-demo-sandbox")
location = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
# Discovery Engine only supports "global", "us", and "eu" locations. Map other regions to "global".
if location not in ("global", "us", "eu"):
    location = "global"
data_store_id = os.getenv("RAG_DATA_STORE_ID", "confluence-knowledge-store")

# Construct the full data store resource path required by DiscoveryEngineSearchTool
full_data_store_path = f"projects/{project_id}/locations/{location}/collections/default_collection/dataStores/{data_store_id}"

# Initialize the discovery engine search tool
_search_tool = DiscoveryEngineSearchTool(
    data_store_id=full_data_store_path,
    search_result_mode=SearchResultMode.DOCUMENTS
)


def _get_mock_search_results(query_str: str) -> str:
    """Performs keyword matching against fallback datasets when Discovery Engine is not live."""
    from rag_agent.ingest import get_mock_confluence_data, get_mock_sharepoint_data

    docs = get_mock_confluence_data() + get_mock_sharepoint_data()

    matched = []
    query_words = query_str.lower().split()
    for doc in docs:
        content_lower = doc["content"].lower()
        title_lower = doc["title"].lower()
        if any(word in content_lower or word in title_lower for word in query_words):
            matched.append(
                f"### {doc['title']} (Source: {doc['source']} - DEMO MOCK FALLBACK)\n"
                f"URL: {doc['url']}\n"
                f"Content:\n{doc['content']}\n"
            )

    if not matched:
        # Default to onboarding doc if no keywords match, keeping the agent grounded
        doc = docs[0]
        matched.append(
            f"### {doc['title']} (Source: {doc['source']} - DEMO MOCK DEFAULT)\n"
            f"URL: {doc['url']}\n"
            f"Content:\n{doc['content']}\n"
        )

    return "\n---\n".join(matched)


def _query_discovery_engine(query_str: str) -> str:
    # Check for local fallback mode
    try:
        import google.auth
        google.auth.default()
        has_creds = True
    except Exception:
        has_creds = False

    is_local_dummy = os.getenv("DEPLOYMENT_MODE", "local") == "local" and not has_creds

    if is_local_dummy:
        print("[rag_agent] Local fallback mock search triggered.", flush=True)
        return _get_mock_search_results(query_str)

    try:
        res = _search_tool.discovery_engine_search(query_str)
        if res.get("status") == "error":
            print(
                f"[rag_agent] DiscoveryEngineSearchTool call failed: {res.get('error_message')}. Falling back to mock search...",
                flush=True,
            )
            return _get_mock_search_results(query_str)

        formatted_results = []
        for doc in res.get("results", []):
            title = doc.get("title", "Untitled")
            url = doc.get("url", "")
            content = doc.get("content", "")
            formatted_results.append(f"### {title}\nURL: {url}\nContent:\n{content}\n")
        if not formatted_results:
            return "No matching documents found in corporate knowledge base."
        return "\n---\n".join(formatted_results)
    except Exception as e:
        print(
            f"[rag_agent] Exception during search: {e}. Falling back to mock search...",
            flush=True,
        )
        return _get_mock_search_results(query_str)


async def query_company_documents(query: str) -> str:
    """Queries corporate Confluence pages and SharePoint documents indexed in Vertex AI Search to retrieve semantically relevant information."""
    return await asyncio.to_thread(_query_discovery_engine, query)


root_agent = Agent(
    model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    name="rag_agent",
    instruction=AGENT_INSTRUCTION,
    tools=[query_company_documents],
    description=(
        "Specialist retrieval agent: queries corporate documents from SharePoint "
        "and Confluence indexed in Vertex AI Search."
    ),
)
