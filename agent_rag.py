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

# Context variables to track querying session identity and filters thread-safely
current_user_email = contextvars.ContextVar("current_user_email", default="guest@example.com")
current_user_groups = contextvars.ContextVar("current_user_groups", default=[])
current_query_space = contextvars.ContextVar("current_query_space", default=None)
current_query_site = contextvars.ContextVar("current_query_site", default=None)

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

engine_project = os.getenv("GOOGLE_CLOUD_PROJECT", "405625028294")
engine_location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-south1")

# RAG Corpus configuration (target project 405625028294 / us-south1)
rag_project_id = os.getenv("RAG_PROJECT_ID") or "405625028294"
project_id = rag_project_id
rag_corpus_location = os.getenv("RAG_CORPUS_LOCATION", "us-south1")
corpus_id = os.getenv("RAG_CORPUS_ID", "7991637538768945152")
corpus_name = f"projects/{rag_project_id}/locations/{rag_corpus_location}/ragCorpora/{corpus_id}"

print(f"[agent_rag] Initializing global Vertex AI for Reasoning Engine: {engine_project}/{engine_location}", flush=True)
print(f"[agent_rag] Target RAG Corpus: {corpus_name}", flush=True)

vertexai.init(project=engine_project, location=engine_location)

# Standalone, global monkeypatch of google.adk's Runner to strip user_email and auto-create Playground sessions
try:
    from google.adk.runners import Runner
    
    if not getattr(Runner, "_is_patched_for_acl", False):
        original_run_async = Runner.run_async
        original_run = Runner.run
        original_get_or_create = getattr(Runner, "_get_or_create_session", None)
        
        async def patched_get_or_create_session(self, *, user_id: str, session_id: str, get_session_config=None):
            session = await self.session_service.get_session(
                app_name=self.app_name,
                user_id=user_id,
                session_id=session_id,
                config=get_session_config,
            )
            if not session:
                print(f"[agent_rag] Session {session_id} not found; auto-creating new session for user {user_id}", flush=True)
                session = await self.session_service.create_session(
                    app_name=self.app_name, user_id=user_id, session_id=session_id
                )
            return session

        async def patched_run_async(self, *args, **kwargs):
            self.auto_create_session = True
            user_email = kwargs.pop("user_email", "guest@example.com")
            user_groups = kwargs.pop("user_groups", [])
            
            from rag_agent.agent_rag import current_user_email, current_user_groups
            email_token = current_user_email.set(user_email)
            groups_token = current_user_groups.set(user_groups)
            
            try:
                async for event in original_run_async(self, *args, **kwargs):
                    yield event
            finally:
                current_user_email.reset(email_token)
                current_user_groups.reset(groups_token)
                
        def patched_run(self, *args, **kwargs):
            self.auto_create_session = True
            user_email = kwargs.pop("user_email", "guest@example.com")
            user_groups = kwargs.pop("user_groups", [])
            
            from rag_agent.agent_rag import current_user_email, current_user_groups
            email_token = current_user_email.set(user_email)
            groups_token = current_user_groups.set(user_groups)
            
            try:
                return original_run(self, *args, **kwargs)
            finally:
                current_user_email.reset(email_token)
                current_user_groups.reset(groups_token)
                
        Runner.run_async = patched_run_async
        Runner.run = patched_run
        if original_get_or_create:
            Runner._get_or_create_session = patched_get_or_create_session
        Runner._is_patched_for_acl = True
        print("[agent_rag] Successfully applied global ACL & Auto-Session monkeypatch to google.adk.runners.Runner", flush=True)
except Exception as e:
    print(f"[agent_rag] Warning: failed to apply global Runner monkeypatch: {e}", flush=True)

# Load local GCS-to-Confluence and GCS-to-SharePoint URL maps with dynamic GCS fallback
gcs_to_confluence_map: dict[str, str] = {}
gcs_permissions_map: dict[str, dict] = {}
bucket_name = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc")

try:
    from google.cloud import storage
    client = storage.Client(project=project_id)
    bucket = client.bucket(bucket_name)

    # All state mapping files we want to proactively keep fresh from GCS
    sync_files = {
        "confluence_map": ("gcs_confluence_map.json", True),  # (filename, is_url_map)
        "sharepoint_map": ("gcs_sharepoint_map.json", True),
        "confluence_perms": ("gcs_confluence_permissions_map.json", False),
        "sharepoint_perms": ("gcs_sharepoint_permissions_map.json", False),
        "legacy_perms": ("gcs_permissions_map.json", False)
    }

    for key, (filename, is_url_map) in sync_files.items():
        local_path = agent_dir / filename
        # Download from GCS dynamically to ensure stateless correctness in deployed agent
        try:
            blob = bucket.blob(filename)
            if blob.exists():
                blob.download_to_filename(str(local_path))
                print(f"[agent_rag] Freshly downloaded {filename} from GCS bucket {bucket_name}", flush=True)
        except Exception as dl_err:
            print(f"[agent_rag] GCS download failed for {filename} (will use local fallback): {dl_err}", flush=True)

        # Parse and merge contents
        if local_path.exists():
            try:
                import json
                with open(local_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        if is_url_map:
                            gcs_to_confluence_map.update(data)
                            print(f"[agent_rag] Merged {filename} with {len(data)} URL entry(s).", flush=True)
                        else:
                            gcs_permissions_map.update(data)
                            print(f"[agent_rag] Merged permissions {filename} with {len(data)} entry(s).", flush=True)
            except Exception as load_err:
                print(f"[agent_rag] Failed loading/parsing {filename}: {load_err}", flush=True)

except Exception as init_err:
    print(f"[agent_rag] Failed to initialize storage client or load dynamic maps: {init_err}. Using static fallbacks.", flush=True)
    # Static local fallback if GCS client fails (e.g. offline local development)
    for filename, is_url_map in [("gcs_confluence_map.json", True), ("gcs_sharepoint_map.json", True)]:
        local_path = agent_dir / filename
        if local_path.exists():
            try:
                import json
                with open(local_path, "r", encoding="utf-8") as f:
                    gcs_to_confluence_map.update(json.load(f))
            except Exception as e:
                print(f"[agent_rag] Static fallback load failed for {filename}: {e}", flush=True)



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
    # Retrieve current user context threadsafely
    email = current_user_email.get()
    groups = current_user_groups.get()
    target_space = current_query_space.get()
    target_site = current_query_site.get()

    # Programmatic inline extraction of space/site keys if written by user (e.g. space:SEC-COMP or site:exit-lts)
    space_match = re.search(r'\bspace:([a-zA-Z0-9_\-]+)\b', query_str)
    site_match = re.search(r'\bsite:([a-zA-Z0-9_\-]+)\b', query_str)
    
    if space_match:
        target_space = space_match.group(1).strip()
        query_str = re.sub(r'\bspace:[a-zA-Z0-9_\-]+\b', '', query_str).strip()
    if site_match:
        target_site = site_match.group(1).strip()
        query_str = re.sub(r'\bsite:[a-zA-Z0-9_\-]+\b', '', query_str).strip()

    enable_cel = os.getenv("ENABLE_CEL_METADATA_FILTERING", "FALSE").strip().upper() == "TRUE"
    retrieval_config = None

    if enable_cel:
        cel_filter = None
        if groups:
            # Check if user has any privileged groups
            privileged_groups = {"HR", "PAYROLL", "LEGAL", "EXEC_BOARD"}
            user_upper_groups = {g.upper() for g in groups}
            user_priv_groups = user_upper_groups.intersection(privileged_groups)

            if not user_priv_groups:
                # Standard user: strictly filter out restricted documents at database layer
                cel_filter = "restricted == false"
            else:
                # Privileged user: allow public documents OR documents matching their specific privileged department/group
                # e.g., if user is in HR, allow: restricted == false || department == "hr"
                clauses = ["restricted == false"]
                for priv in user_priv_groups:
                    clauses.append(f'department == "{priv.lower()}"')
                cel_filter = " || ".join(clauses)
        else:
            # Default fallback (anonymous/unauthenticated): public documents only
            cel_filter = "restricted == false"

        # Apply specific space/site focus if requested by frontend or parsed from query
        if target_space:
            cel_filter = f"({cel_filter}) && space_name == '{target_space}'"
        if target_site:
            cel_filter = f"({cel_filter}) && site_name == '{target_site}'"

        if cel_filter:
            print(f"[agent_rag] Constructing dynamic CEL metadata filter for user {email}: {cel_filter}", flush=True)
            try:
                from vertexai.preview.rag import RagRetrievalConfig, Filter
                retrieval_config = RagRetrievalConfig(
                    filter=Filter(
                        vector_distance_threshold=0.5,
                        metadata_filter=cel_filter
                    )
                )
            except Exception as e:
                print(f"[agent_rag] Failed to construct RagRetrievalConfig: {e}", flush=True)

    try:
        from google.cloud import aiplatform_v1beta1

        api_filter = aiplatform_v1beta1.RagRetrievalConfig.Filter(vector_distance_threshold=0.5)
        if enable_cel and 'cel_filter' in locals() and cel_filter:
            api_filter.metadata_filter = cel_filter

        api_retrieval_config = aiplatform_v1beta1.RagRetrievalConfig(
            top_k=5,
            filter=api_filter,
        )

        request = aiplatform_v1beta1.RetrieveContextsRequest(
            vertex_rag_store=aiplatform_v1beta1.RetrieveContextsRequest.VertexRagStore(
                rag_resources=[
                    aiplatform_v1beta1.RetrieveContextsRequest.VertexRagStore.RagResource(
                        rag_corpus=corpus_name
                    )
                ]
            ),
            parent=f"projects/{rag_project_id}/locations/{rag_corpus_location}",
            query=aiplatform_v1beta1.RagQuery(
                text=query_str,
                rag_retrieval_config=api_retrieval_config,
            ),
        )

        client_options = {"api_endpoint": f"{rag_corpus_location}-aiplatform.googleapis.com"}
        rag_client = aiplatform_v1beta1.VertexRagServiceClient(client_options=client_options)
        response = rag_client.retrieve_contexts(request=request)
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
