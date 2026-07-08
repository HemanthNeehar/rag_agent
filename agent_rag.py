import os
import re
import asyncio
import contextvars
from pathlib import Path
from dotenv import load_dotenv

import vertexai
from vertexai.preview import rag
from google.adk.agents import Agent

agent_dir = Path(__file__).parent
load_dotenv(agent_dir.parent / ".env")

# Context variables to track querying session identity thread-safely
current_user_email = contextvars.ContextVar("current_user_email", default="guest@example.com")
current_user_groups = contextvars.ContextVar("current_user_groups", default=[])

## Load instruction from markdown file
instruction_file = agent_dir / "INSTRUCTION.md"
with open(instruction_file, "r", encoding="utf-8") as f:
    AGENT_INSTRUCTION = f.read()

## Service account authentication from env
service_account_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH")
if service_account_path:
    if not os.path.isabs(service_account_path):
        service_account_path = str(agent_dir / service_account_path)
    if Path(service_account_path).exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = service_account_path
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"

project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "gebu-demo-sandbox")
location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
corpus_id = os.getenv("RAG_CORPUS_ID", "6301661778598166528")
corpus_name = f"projects/{project_id}/locations/{location}/ragCorpora/{corpus_id}"

vertexai.init(project=project_id, location=location)

# Load local GCS-to-Confluence and GCS-to-SharePoint URL maps for foolproof source URL resolution
gcs_map_file = agent_dir / "gcs_confluence_map.json"
gcs_to_confluence_map: dict[str, str] = {}
if gcs_map_file.exists():
    try:
        import json
        with open(gcs_map_file, "r", encoding="utf-8") as f:
            gcs_to_confluence_map = json.load(f)
    except Exception as e:
        print(f"[agent_rag] Failed to load gcs_confluence_map.json: {e}", flush=True)

gcs_sp_map_file = agent_dir / "gcs_sharepoint_map.json"
if gcs_sp_map_file.exists():
    try:
        import json
        with open(gcs_sp_map_file, "r", encoding="utf-8") as f:
            gcs_to_confluence_map.update(json.load(f))
    except Exception as e:
        print(f"[agent_rag] Failed to load gcs_sharepoint_map.json: {e}", flush=True)

# Load Permissions Map with GCS dynamic download fallback to ensure stateless resilience
gcs_permissions_map: dict[str, dict] = {}
gcs_perm_file = agent_dir / "gcs_permissions_map.json"

if not gcs_perm_file.exists():
    bucket_name = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc")
    try:
        from google.cloud import storage
        client = storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob("gcs_permissions_map.json")
        if blob.exists():
            blob.download_to_filename(str(gcs_perm_file))
            print(f"[agent_rag] Successfully downloaded gcs_permissions_map.json from GCS bucket {bucket_name}", flush=True)
    except Exception as e:
        print(f"[agent_rag] Failed downloading gcs_permissions_map.json from GCS: {e}", flush=True)

if gcs_perm_file.exists():
    try:
        import json
        with open(gcs_perm_file, "r", encoding="utf-8") as f:
            gcs_permissions_map = json.load(f)
        print(f"[agent_rag] Loaded gcs_permissions_map.json with {len(gcs_permissions_map)} entry(s).", flush=True)
    except Exception as e:
        print(f"[agent_rag] Failed to load gcs_permissions_map.json: {e}", flush=True)


def check_user_access(filename: str, email: str, groups: list[str]) -> bool:
    """Verifies if the current user has access to the specified file based on the permissions map.
    Returns True if access is granted, False if restricted. Non-listed files default to public (True)."""
    if not filename:
        return True
    
    # Normalize filename keys (case/space/delimiter-insensitive fallback)
    perm = gcs_permissions_map.get(filename)
    if not perm:
        f_norm = filename.lower().replace(" ", "_")
        for k, v in gcs_permissions_map.items():
            if k.lower().replace(" ", "_") == f_norm:
                perm = v
                break
                
    if not perm:
        return True  # Backwards compatibility: files not listed are public
        
    restricted = perm.get("restricted", False)
    if not restricted:
        return True
        
    # Check email (case-insensitive)
    allowed_users = [u.lower() for u in perm.get("allowed_users", [])]
    if email.lower() in allowed_users:
        return True
        
    # Check groups (case-insensitive)
    allowed_groups = [g.lower() for g in perm.get("allowed_groups", [])]
    user_groups_lower = [g.lower() for g in groups]
    for g in user_groups_lower:
        if g in allowed_groups:
            return True
            
    return False



def _extract_source_url(text: str) -> str | None:
    """Pulls the source_url metadata line embedded at the top of each ingested document."""
    m = re.search(r'^source_url:\s*(.+)$', text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _query_rag_corpus(query_str: str) -> str:
    """Queries the Vertex AI RAG corpus and returns formatted results with source links and images."""
    try:
        response = rag.retrieval_query(
            rag_resources=[
                rag.RagResource(rag_corpus=corpus_name)
            ],
            text=query_str,
            similarity_top_k=5,
            vector_distance_threshold=0.5,
        )
    except Exception as e:
        print(f"[rag_agent_gcs] RAG corpus query failed: {e}", flush=True)
        return f"Search failed: {e}"

    contexts = response.contexts.contexts if response.contexts else []
    if not contexts:
        return "No matching documents found in the corporate knowledge base."

    bucket_name = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc")

    # Retrieve current user context threadsafely
    email = current_user_email.get()
    groups = current_user_groups.get()

    # --- Pass 1: collect source URLs from ALL returned chunks first.
    # Mid-document chunks may not contain the header source_url line, but another
    # chunk from the same GCS file might.  Build a map: gcs_uri -> confluence_url
    # so every chunk can be attributed even if its text window lacks the header.
    gcs_to_confluence: dict[str, str] = {}
    for ctx in contexts:
        gcs_uri = ctx.source_uri or ""
        gcs_filename = gcs_uri.split("/")[-1] if gcs_uri else ""
        if not check_user_access(gcs_filename, email, groups):
            continue
        url = _extract_source_url(ctx.text or "")
        if url and gcs_uri:
            gcs_to_confluence[gcs_uri] = url

    # --- Pass 2: build result blocks with guaranteed source attribution.
    results = []
    for ctx in contexts:
        gcs_uri = ctx.source_uri or ""
        gcs_filename = gcs_uri.split("/")[-1] if gcs_uri else ""
        if not check_user_access(gcs_filename, email, groups):
            print(f"[agent_rag] Redacting restricted chunk {gcs_filename} from user {email} (groups: {groups})", flush=True)
            continue

        text = ctx.text or ""

        # Prefer embedded source_url; fall back to our local mapping JSON, then cross-chunk map, and finally GCS path
        source_url = (
            _extract_source_url(text)
            or gcs_to_confluence_map.get(gcs_filename, "")
            or gcs_to_confluence.get(gcs_uri, "")
            or gcs_uri
        )
        if source_url.startswith("httpss://"):
            source_url = source_url.replace("httpss://", "https://")

        # Convert any relative image paths still present to full GCS public URLs
        text = re.sub(
            r'!\[([^\]]*)\]\(images/([^)]+)\)',
            lambda m: f"![{m.group(1)}](https://storage.googleapis.com/{bucket_name}/images/{m.group(2)})",
            text,
        )

        # Parse page title from metadata before stripping
        title_match = re.search(r'^title:\s*(.+)$', text, re.MULTILINE)
        title_text = title_match.group(1).strip() if title_match else "Source Page"
        if "![" in title_text:
            m_img = re.search(r'!\[([^\]]*)\]', title_text)
            if m_img:
                title_text = m_img.group(1) or "Image"
        title_text = re.sub(r'\.(png|jpg|jpeg|gif|svg|webp)(\.md)?$', '', title_text, flags=re.IGNORECASE)
        title_text = title_text.replace("_", " ").strip()

        # Strip metadata lines from visible text
        display_text = re.sub(r'^(source_url|title|source_system|doc_id):[^\n]*\n?', '', text, flags=re.MULTILINE).strip()

        # Prevent tool call truncation by imposing a maximum safe chunk size
        max_chunk_chars = 3000
        if len(display_text) > max_chunk_chars:
            display_text = display_text[:max_chunk_chars] + "\n\n... *[Content truncated for tool call size compatibility]*"

        score = getattr(ctx, 'score', None)
        score_str = f" *(relevance: {score:.2f})*" if score is not None else ""

        # Show as clickable link with page title when we have a real URL; GCS path otherwise
        if source_url.startswith("http"):
            source_line = f"[🔗 {title_text}]({source_url}){score_str}"
        else:
            source_line = f"`{source_url}`{score_str}"  # GCS path as code span

        block = f"**Source:** {source_line}\n\n{display_text}"
        results.append(block)

    final_output = "\n\n---\n\n".join(results)
    # Impose a total size limit to avoid blowing past API gateway function response buffer limit (approx 15KB)
    max_total_chars = 15000
    if len(final_output) > max_total_chars:
        final_output = final_output[:max_total_chars] + "\n\n... *[Remaining sources truncated due to payload length limits]*"
    return final_output


async def query_company_documents(query: str) -> str:
    """Queries corporate Confluence and SharePoint documents stored in the Vertex AI RAG corpus.
    Returns relevant content chunks with source page links and any embedded images."""
    return await asyncio.to_thread(_query_rag_corpus, query)


root_agent = Agent(
    model=os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
    name="rag_agent_gcs",
    instruction=AGENT_INSTRUCTION,
    tools=[query_company_documents],
    description=(
        "Specialist retrieval agent: queries corporate Confluence and SharePoint documents "
        "ingested into the Vertex AI RAG corpus. Returns source page links and images."
    ),
)
