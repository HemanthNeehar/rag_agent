"""RAG Knowledge Chat — FastAPI backend + embedded single-page UI.

Serves a chat interface that:
  1. Calls the deployed AdkApp (ReasoningEngine) via vertexai client
  2. Renders markdown responses including inline GCS images
  3. Falls back to direct rag.retrieval_query() if no deployed engine is configured

Run locally:
    python -m rag_agent.chat_server
    # Opens on http://localhost:8020

Environment variables (from .env):
    GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION
    RAG_AGENT_RESOURCE_NAME  — the deployed AdkApp resource name (optional)
    RAG_CORPUS_ID            — fallback: query RAG corpus directly
    RAG_GCS_BUCKET_NAME      — used to build public image URLs
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid

class SafeBuffer:
    def __init__(self, original_buffer, log_filepath):
        self.original_buffer = original_buffer
        self.log_filepath = log_filepath

    def write(self, data):
        try:
            if self.original_buffer:
                return self.original_buffer.write(data)
        except OSError as e:
            if e.errno == 5:
                try:
                    with open(self.log_filepath, "ab") as f:
                        f.write(data)
                except Exception:
                    pass
                return len(data)
            else:
                raise

    def flush(self):
        try:
            if self.original_buffer and hasattr(self.original_buffer, "flush"):
                self.original_buffer.flush()
        except OSError as e:
            if e.errno == 5:
                pass
            else:
                raise

    def __getattr__(self, name):
        return getattr(self.original_buffer, name)


class SafeStream:
    def __init__(self, original_stream, log_filepath):
        self.original_stream = original_stream
        self.log_filepath = log_filepath
        if original_stream and hasattr(original_stream, "buffer") and original_stream.buffer:
            self.buffer = SafeBuffer(original_stream.buffer, log_filepath)
        else:
            self.buffer = None

    def write(self, data):
        try:
            if self.original_stream:
                self.original_stream.write(data)
        except OSError as e:
            if e.errno == 5:  # Input/output error
                try:
                    with open(self.log_filepath, "a", encoding="utf-8") as f:
                        f.write(data)
                except Exception:
                    pass
            else:
                raise

    def flush(self):
        try:
            if self.original_stream and hasattr(self.original_stream, "flush"):
                self.original_stream.flush()
        except OSError as e:
            if e.errno == 5:
                pass
            else:
                raise

    def __getattr__(self, name):
        return getattr(self.original_stream, name)

sys.stdout = SafeStream(sys.stdout, "/home/hemanth_gadavajhala/chat_server_stdout.log")
sys.stderr = SafeStream(sys.stderr, "/home/hemanth_gadavajhala/chat_server_stderr.log")
sys.__stdout__ = sys.stdout
sys.__stderr__ = sys.stderr

from pathlib import Path
from typing import AsyncIterator

import uvicorn
import vertexai
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from vertexai.preview import rag

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
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
RESOURCE_NAME = os.getenv("RAG_AGENT_RESOURCE_NAME", "")
if RESOURCE_NAME and RESOURCE_NAME.strip().lower() in ("", "none", "null"):
    RESOURCE_NAME = ""
CORPUS_ID = os.getenv("RAG_CORPUS_ID", "")
if CORPUS_ID and CORPUS_ID.strip().lower() in ("", "none", "null"):
    CORPUS_ID = ""
BUCKET = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc")

vertexai.init(project=PROJECT, location=LOCATION)

# Load local GCS-to-Confluence and GCS-to-SharePoint URL maps for fallback direct RAG queries
gcs_map_file = Path(__file__).parent / "gcs_confluence_map.json"
gcs_to_confluence_map: dict[str, str] = {}
if gcs_map_file.exists():
    try:
        with open(gcs_map_file, "r", encoding="utf-8") as f:
            gcs_to_confluence_map = json.load(f)
    except Exception as e:
        print(f"[rag_chat] Failed to load gcs_confluence_map.json: {e}")

gcs_sp_map_file = Path(__file__).parent / "gcs_sharepoint_map.json"
if gcs_sp_map_file.exists():
    try:
        with open(gcs_sp_map_file, "r", encoding="utf-8") as f:
            gcs_to_confluence_map.update(json.load(f))
    except Exception as e:
        print(f"[rag_chat] Failed to load gcs_sharepoint_map.json: {e}")

# Global GCS image proxy cache and robust mapping system
gcs_images_cache: dict[str, str] = {}
image_to_source_map: dict[str, str] = {}


def _normalize_image_key(filename: str) -> str:
    """Fuzzy normalizes image filenames to map them case/space/delimiter-insensitively."""
    name, ext = os.path.splitext(filename.lower())
    # Strip any non-alphanumeric characters to make lookup incredibly resilient
    clean_name = re.sub(r'[^a-z0-9]', '', name)
    return f"{clean_name}{ext}"


def _build_gcs_cache_and_source_map():
    print("[rag_chat] Initializing GCS image cache and source map...")
    try:
        from google.cloud import storage
        client = storage.Client(project=PROJECT)
        bucket = client.bucket(BUCKET)
        
        blobs = bucket.list_blobs(prefix="images/")
        count = 0
        for blob in blobs:
            full_name = blob.name
            if not full_name.startswith("images/"):
                continue
            filename = full_name[len("images/"):]
            if not filename:
                continue
                
            norm_key = _normalize_image_key(filename)
            gcs_images_cache[norm_key] = filename
            count += 1
            
            # Auto-map Confluence images using their page ID prefix (confluence_1507329_image...)
            conf_match = re.match(r"^confluence_(\d+)_", filename, re.IGNORECASE)
            if conf_match:
                page_id = conf_match.group(1)
                source_url = f"https://hemanthgadavajhala.atlassian.net/wiki/spaces/AD/pages/{page_id}"
                image_to_source_map[norm_key] = source_url
                
                # Also index the stripped raw name (image-20260609-094553.png -> confluence_1507329_image-20260609-094553.png)
                stripped_name = filename[conf_match.end():]
                stripped_norm_key = _normalize_image_key(stripped_name)
                gcs_images_cache[stripped_norm_key] = filename
                image_to_source_map[stripped_norm_key] = source_url
                
        # Now associate SharePoint / other images using the loaded maps (using doc key filename mapping)
        for doc_key, source_url in gcs_to_confluence_map.items():
            base_doc = doc_key[:-3] if doc_key.lower().endswith(".md") else doc_key
            base_norm_key = _normalize_image_key(base_doc)
            if base_norm_key in gcs_images_cache:
                image_to_source_map[base_norm_key] = source_url
                actual_gcs_file = gcs_images_cache[base_norm_key]
                actual_norm_key = _normalize_image_key(actual_gcs_file)
                image_to_source_map[actual_norm_key] = source_url
                
        print(f"[rag_chat] GCS image cache initialized: cached {count} files, mapped {len(image_to_source_map)} images to source documents.")
    except Exception as e:
        print(f"[rag_chat] Failed to build GCS image cache: {e}")


# Run the cache builder on startup
_build_gcs_cache_and_source_map()

_created_sessions: set[str] = set()

app = FastAPI(title="RAG Knowledge Chat")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Query helpers ─────────────────────────────────────────────────────────────

def _sanitize_session_id(session_id: str) -> str:
    if not session_id:
        return ""
    # Convert underscores to hyphens and lowercase
    sanitized = session_id.lower().replace("_", "-")
    # Retain only lowercase alphanumeric characters and hyphens
    sanitized = re.sub(r'[^a-z0-9-]', '', sanitized)
    # Ensure it doesn't start or end with a hyphen
    sanitized = sanitized.strip("-")
    return sanitized


def _gcs_to_public_url(text: str) -> str:
    """Rewrites relative, public GCS, Confluence, or SharePoint image references to use our secure local backend proxy."""
    # Fix double-s typo in secure protocols (httpss:// -> https://)
    text = text.replace("httpss://", "https://")

    # 0. Clean accidental single/double quotes around URLs in markdown links/images, e.g. [text]('url') -> [text](url)
    text = re.sub(r"\]\(['\"]([^\)]*?)['\"]\)", r"](\1)", text)
    text = re.sub(r"(!?\[)['\"]([^\]]*?)['\"](\])", r"\1\2\3", text)

    # 1. Rewrite backticked raw image filename mentions to markdown images
    text = re.sub(
        r'`([a-zA-Z0-9_-]+\.(?:png|jpg|jpeg|gif|svg|webp))`',
        r"![\1](/api/images/\1)",
        text,
    )
    # 2. Rewrite bracketed raw image filename mentions (like [architecture-bank-alpha.png]) to markdown images
    text = re.sub(
        r"(?<!\!)(?<!\()\[([a-zA-Z0-9_-]+\.(?:png|jpg|jpeg|gif|svg|webp))\](?!\()",
        r"![\1](/api/images/\1)",
        text,
    )
    # 3. Rewrite raw image filename mentions (like architecture-bank-alpha.png or !image-20260609.png)
    pattern = r"(!?)(?<![a-zA-Z0-9_\/\[\(\-\`])([a-zA-Z0-9_-]+\.(?:png|jpg|jpeg|gif|svg|webp))\b(?![a-zA-Z0-9_\)\]\-\`])"
    text = re.sub(
        pattern,
        r'![Diagram](/api/images/\2)',
        text,
    )

    # 4. Comprehensive markdown image rewrite rule matching any ![...] (url/path)
    img_exts = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
    
    def extract_filename(s):
        import urllib.parse
        decoded = urllib.parse.unquote(s)
        base = decoded.split("?")[0].split("#")[0]
        filename = base.split("/")[-1]
        if filename.lower().endswith(img_exts):
            return filename
        return None

    def rewrite_image_markdown(match):
        alt = match.group(1)
        url = match.group(2)
        filename = extract_filename(url) or extract_filename(alt)
        if filename:
            return f"![{alt}](/api/images/{filename})"
        return match.group(0)

    # Match all markdown images/links with optional ! prefix: ![alt](url) or [alt_with_image_ext](url)
    text = re.sub(r"!?\[([^\]]*)\]\(([^)]+)\)", rewrite_image_markdown, text)
    return text


def _get_gcs_image(filename: str) -> bytes:
    """Fetches image bytes securely from the Google Cloud Storage bucket."""
    from google.cloud import storage
    client = storage.Client(project=PROJECT)
    bucket = client.bucket(BUCKET)
    blob = bucket.blob(f"images/{filename}")
    return blob.download_as_bytes()


def _strip_metadata_lines(text: str) -> str:
    cleaned = re.sub(r'^(source_url|title|source_system|doc_id):[^\n]*\n?', '', text, flags=re.MULTILINE)
    cleaned = re.sub(r'^\[IMAGE CAPTION:[^\n]*\n?', '', cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r'^\[IMAGE CONTENT:[^\n]*\n?', '', cleaned, flags=re.MULTILINE)
    return cleaned.strip()


def _query_via_deployed_agent(query: str, session_id: str = "", user_email: str = "guest@example.com", user_groups: list[str] = []) -> str:
    """Calls the deployed AdkApp ReasoningEngine via direct gRPC streaming execution."""
    from vertexai.preview.reasoning_engines import ReasoningEngine
    from google.cloud.aiplatform_v1beta1.types import StreamQueryReasoningEngineRequest
    
    # Initialize the reasoning engine
    engine = ReasoningEngine(RESOURCE_NAME)
    
    # Maintain user sessions
    global _created_sessions
    if session_id and session_id not in _created_sessions:
        try:
            client = vertexai.Client(project=PROJECT, location=LOCATION)
            remote_app = client.agent_engines.get(name=RESOURCE_NAME)
            remote_app.create_session(session_id=session_id, user_id="default-user")
            _created_sessions.add(session_id)
            print(f"[rag_chat] Sync session {session_id} created/verified.")
        except Exception as ex:
            if "already exists" in str(ex).lower():
                _created_sessions.add(session_id)
                print(f"[rag_chat] Sync session {session_id} already exists on backend. Reusing.")
            else:
                print(f"[rag_chat] Sync session creation failed/skipped: {ex}")
        
    request = StreamQueryReasoningEngineRequest(
        name=engine.resource_name,
        input={
            "message": query,
            "user_id": "default-user",
            "session_id": session_id if session_id else "",
            "user_email": user_email,
            "user_groups": user_groups
        },
        class_method="stream_query"
    )
    
    # Direct streaming API call to retrieve response chunks
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
            except Exception:
                pass
                
    answer = "".join(text_parts)
    return _gcs_to_public_url(answer)


def _query_via_rag_corpus(query: str) -> str:
    """Direct RAG corpus query — used when no deployed engine is configured."""
    from rag_agent.agent_rag import check_user_access, current_user_email, current_user_groups
    email = current_user_email.get()
    groups = current_user_groups.get()

    corpus_name = f"projects/{PROJECT}/locations/{LOCATION}/ragCorpora/{CORPUS_ID}"
    response = rag.retrieval_query(
        rag_resources=[rag.RagResource(rag_corpus=corpus_name)],
        text=query,
        similarity_top_k=5,
        vector_distance_threshold=0.5,
    )
    contexts = response.contexts.contexts if response.contexts else []
    if not contexts:
        return "No matching documents found in the knowledge base."

    blocks = []
    for ctx in contexts:
        gcs_uri = ctx.source_uri or ""
        gcs_filename = gcs_uri.split("/")[-1] if gcs_uri else ""
        if not check_user_access(gcs_filename, email, groups):
            print(f"[chat_server] Redacting restricted chunk {gcs_filename} from user {email} (groups: {groups})", flush=True)
            continue

        # Extract title from raw context text before stripping metadata lines
        title_match = re.search(r'^title:\s*(.+)$', ctx.text or "", re.MULTILINE)
        title_text = title_match.group(1).strip() if title_match else "Source Page"
        if "![" in title_text:
            m_img = re.search(r'!\[([^\]]*)\]', title_text)
            if m_img:
                title_text = m_img.group(1) or "Image"
        title_text = re.sub(r'\.(png|jpg|jpeg|gif|svg|webp)(\.md)?$', '', title_text, flags=re.IGNORECASE)
        title_text = title_text.replace("_", " ").strip()

        text = _strip_metadata_lines(_gcs_to_public_url(ctx.text or ""))
        
        # Prevent tool/fallback truncation by imposing a maximum safe chunk size
        max_chunk_chars = 3000
        if len(text) > max_chunk_chars:
            text = text[:max_chunk_chars] + "\n\n... *[Content truncated for size compatibility]*"
        
        # Pull page details using local filename mapping, falling back to embedded metadata
        gcs_uri = ctx.source_uri or ""
        gcs_filename = gcs_uri.split("/")[-1] if gcs_uri else ""
        source = re.search(r'^source_url:\s*(.+)$', ctx.text or "", re.MULTILINE)
        source_url = (
            gcs_to_confluence_map.get(gcs_filename)
            or (source.group(1).strip() if source else "")
            or gcs_uri
        )
        
        score = getattr(ctx, "score", None)
        score_str = f" *(relevance: {score:.2f})*" if score else ""
        
        # Show as clickable link with page title when we have a real URL
        if source_url.startswith("http"):
            header = f"**Source:** [{title_text}]({source_url}){score_str}"
        else:
            header = f"**Source:** `{source_url}`{score_str}"
        blocks.append(f"{header}\n\n{text}" if header else text)

    final_output = "\n\n---\n\n".join(blocks)
    max_total_chars = 15000
    if len(final_output) > max_total_chars:
        final_output = final_output[:max_total_chars] + "\n\n... *[Remaining sources truncated due to payload length limits]*"
    return final_output


async def _run_query(query: str, session_id: str = "", user_email: str = "guest@example.com", user_groups: list[str] = []) -> str:
    """Picks the best available query path."""
    from rag_agent.agent_rag import current_user_email, current_user_groups
    email_token = current_user_email.set(user_email)
    groups_token = current_user_groups.set(user_groups)
    try:
        if RESOURCE_NAME:
            return await asyncio.to_thread(_query_via_deployed_agent, query, session_id, user_email, user_groups)
        elif CORPUS_ID:
            return await asyncio.to_thread(_query_via_rag_corpus, query)
        else:
            return "No RAG_AGENT_RESOURCE_NAME or RAG_CORPUS_ID configured in .env"
    except Exception as e:
        return f"**Error:** {e}"
    finally:
        current_user_email.reset(email_token)
        current_user_groups.reset(groups_token)


async def _stream_query(query: str, session_id: str = "", user_email: str = "guest@example.com", user_groups: list[str] = []) -> AsyncIterator[str]:
    """SSE stream: yields `data: <json>\n\n` events."""
    # Yield an immediate empty token to flush connection headers and satisfy browser timeouts
    yield "data: " + json.dumps({"token": ""}) + "\n\n"

    from rag_agent.agent_rag import current_user_email, current_user_groups
    email_token = current_user_email.set(user_email)
    groups_token = current_user_groups.set(user_groups)

    try:
        if RESOURCE_NAME:
            try:
                from vertexai.preview.reasoning_engines import ReasoningEngine
                from google.cloud.aiplatform_v1beta1.types import StreamQueryReasoningEngineRequest
                
                engine = ReasoningEngine(RESOURCE_NAME)
                
                global _created_sessions
                if session_id and session_id not in _created_sessions:
                    try:
                        client = vertexai.Client(project=PROJECT, location=LOCATION)
                        remote_app = client.agent_engines.get(name=RESOURCE_NAME)
                        remote_app.create_session(session_id=session_id, user_id="default-user")
                        _created_sessions.add(session_id)
                        print(f"[rag_chat] Handshook session {session_id} successfully.")
                    except Exception as ex:
                        if "already exists" in str(ex).lower():
                            _created_sessions.add(session_id)
                            print(f"[rag_chat] Session {session_id} already exists on backend. Reusing.")
                        else:
                            print(f"[rag_chat] Session handshake failed/skipped: {ex}")

                request = StreamQueryReasoningEngineRequest(
                    name=engine.resource_name,
                    input={
                        "message": query,
                        "user_id": "default-user",
                        "session_id": session_id if session_id else "",
                        "user_email": user_email,
                        "user_groups": user_groups
                    },
                    class_method="stream_query"
                )
                
                def run_stream():
                    return engine.execution_api_client.stream_query_reasoning_engine(request=request)
                    
                response = await asyncio.to_thread(run_stream)
                
                for chunk in response:
                    if chunk.data:
                        try:
                            data = json.loads(chunk.data)
                            parts = data.get("content", {}).get("parts", [])
                            for p in parts:
                                if "text" in p and isinstance(p, dict):
                                    text = p["text"]
                                    # Convert GCS relative image URLs to public URLs
                                    text = _gcs_to_public_url(text)
                                    yield f"data: {json.dumps({'token': text})}\n\n"
                        except Exception:
                            pass
            except Exception as e:
                yield f"data: {json.dumps({'token': f'\\n\\n**Error during streaming:** {e}\\n'})}\n\n"
            
            yield "data: {\"done\": true}\n\n"
        else:
            # Direct RAG corpus lookup fallback
            try:
                answer = await asyncio.to_thread(_query_via_rag_corpus, query)
            except Exception as e:
                answer = f"**Error:** {e}"

            # Stream chunk-by-chunk for direct RAG fallback
            words = answer.split(" ")
            for i, word in enumerate(words):
                token = word
                if i < len(words) - 1:
                    token += " "
                yield f"data: {json.dumps({'token': token})}\n\n"
                if (i + 1) % 4 == 0:
                    await asyncio.sleep(0.02)
            yield "data: {\"done\": true}\n\n"
    finally:
        current_user_email.reset(email_token)
        current_user_groups.reset(groups_token)


# ── API endpoints ─────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    query: str
    session_id: str = ""
    user_email: str = "guest@example.com"
    user_groups: list[str] = []


@app.get("/api/config")
def get_config():
    return {
        "project": PROJECT,
        "location": LOCATION,
        "resource_name": RESOURCE_NAME,
        "corpus_id": CORPUS_ID,
        "mode": "deployed_agent" if RESOURCE_NAME else ("rag_corpus" if CORPUS_ID else "unconfigured"),
    }


@app.get("/api/mappings")
def get_mappings():
    # Return merged dictionary of document maps and dynamic image source maps
    merged = {}
    merged.update(gcs_to_confluence_map)
    merged.update(image_to_source_map)
    return merged


@app.post("/api/query")
async def query_endpoint(req: QueryRequest):
    session_id = _sanitize_session_id(req.session_id) or _sanitize_session_id(str(uuid.uuid4()))
    answer = await _run_query(req.query, session_id, user_email=req.user_email, user_groups=req.user_groups)
    return {"answer": answer, "session_id": session_id}


@app.get("/api/stream")
async def stream_endpoint(query: str, session_id: str = "", user_email: str = "guest@example.com", user_groups: str = "", request: Request = None):
    """Server-Sent Events endpoint for streaming responses."""
    sanitized_session_id = _sanitize_session_id(session_id)
    groups_list = [g.strip() for g in user_groups.split(",") if g.strip()] if user_groups else []
    
    async def event_generator():
        async for chunk in _stream_query(query, sanitized_session_id, user_email=user_email, user_groups=groups_list):
            if request and await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/images/{filename}")
async def get_image_endpoint(filename: str):
    """Secure GCS image proxy endpoint to bypass CORS and bucket permissions limitations."""
    import urllib.parse
    decoded_filename = urllib.parse.unquote(filename)
    
    # Strip forbidden characters: \ / * ? : " < > | ' `
    sanitized_filename = re.sub(r'[\\/*?:"<>|\'`]', "", decoded_filename)
    
    # Resolve the requested filename case/space/delimiter-insensitively via the startup cache
    norm_key = _normalize_image_key(sanitized_filename)
    actual_filename = gcs_images_cache.get(norm_key, sanitized_filename)
    
    try:
        loop = asyncio.get_running_loop()
        content = await loop.run_in_executor(None, _get_gcs_image, actual_filename)
        
        media_type = "image/png"
        ext = sanitized_filename.lower()
        if ext.endswith(".jpg") or ext.endswith(".jpeg"):
            media_type = "image/jpeg"
        elif ext.endswith(".gif"):
            media_type = "image/gif"
        elif ext.endswith(".svg"):
            media_type = "image/svg+xml"
            
        return Response(content=content, media_type=media_type)
    except Exception as e:
        print(f"[rag_chat] Failed to proxy image {filename} from GCS: {e}")
        return Response(status_code=404, content=f"Image {filename} not found in GCS bucket {BUCKET}")


# ── Single-page frontend ───────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RAG Knowledge Chat</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --border: rgba(255,255,255,0.08);
    --accent: #4f8ef7;
    --accent2: #7c3aed;
    --text: #e8eaf0;
    --muted: #8b92a4;
    --user-bg: #1e2a45;
    --agent-bg: #1a1d27;
    --code-bg: #12141c;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); height: 100dvh; display: flex; flex-direction: column; }

  /* Header */
  header { display: flex; align-items: center; gap: 12px; padding: 14px 20px; border-bottom: 1px solid var(--border); background: var(--surface); flex-shrink: 0; }
  .logo { width: 36px; height: 36px; background: linear-gradient(135deg, var(--accent), var(--accent2)); border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 18px; }
  header h1 { font-size: 17px; font-weight: 600; }
  header .badge { margin-left: auto; padding: 4px 10px; border-radius: 20px; font-size: 11px; font-weight: 500; }
  .badge.agent { background: rgba(79,142,247,0.15); color: var(--accent); border: 1px solid rgba(79,142,247,0.3); }
  .badge.corpus { background: rgba(124,58,237,0.15); color: #a78bfa; border: 1px solid rgba(124,58,237,0.3); }
  .badge.unconfigured { background: rgba(239,68,68,0.1); color: #f87171; border: 1px solid rgba(239,68,68,0.3); }

  /* Chat area */
  #chat { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px; }
  #chat::-webkit-scrollbar { width: 6px; } #chat::-webkit-scrollbar-track { background: transparent; } #chat::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }

  /* Messages */
  .msg { display: flex; gap: 10px; max-width: 900px; width: 100%; }
  .msg.user { align-self: flex-end; flex-direction: row-reverse; }
  .msg.agent { align-self: flex-start; }
  .avatar { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; margin-top: 2px; }
  .msg.user .avatar { background: linear-gradient(135deg, var(--accent), var(--accent2)); }
  .msg.agent .avatar { background: #252836; border: 1px solid var(--border); }
  .bubble { padding: 12px 16px; border-radius: 14px; max-width: calc(100% - 44px); line-height: 1.6; font-size: 14px; }
  .msg.user .bubble { background: var(--user-bg); border-radius: 14px 4px 14px 14px; }
  .msg.agent .bubble { background: var(--agent-bg); border: 1px solid var(--border); border-radius: 4px 14px 14px 14px; }

  /* Markdown inside bubble */
  .bubble h1,.bubble h2,.bubble h3 { margin: 14px 0 6px; font-size: 15px; color: var(--accent); }
  .bubble p { margin: 6px 0; }
  .bubble ul,.bubble ol { margin: 6px 0 6px 20px; }
  .bubble li { margin: 3px 0; }
  .bubble code { background: var(--code-bg); padding: 2px 5px; border-radius: 4px; font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #e879f9; }
  .bubble pre { background: var(--code-bg); padding: 12px; border-radius: 8px; overflow-x: auto; margin: 8px 0; }
  .bubble pre code { background: none; padding: 0; color: #e2e8f0; font-size: 12px; }
  .bubble a { color: var(--accent); text-decoration: none; }
  .bubble a:hover { text-decoration: underline; }
  .bubble strong { color: #c7d2fe; }
  .bubble hr { border: none; border-top: 1px solid var(--border); margin: 12px 0; }

  /* Images in responses and wrappers */
  .img-wrapper {
    margin: 16px 0;
    display: inline-block;
    max-width: 100%;
    vertical-align: top;
  }
  .img-wrapper img {
    max-height: 380px !important;
    max-width: 100% !important;
    border-radius: 8px !important;
    border: 1px solid var(--border) !important;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.25s ease !important;
    cursor: zoom-in !important;
    display: block !important;
    margin: 0 !important;
  }
  .img-wrapper img:hover {
    transform: translateY(-2px) scale(1.01) !important;
    box-shadow: 0 8px 24px rgba(0,0,0,0.25) !important;
  }
  .img-wrapper-badge {
    margin-top: 8px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .img-badge-link {
    transition: all 0.2s ease !important;
  }
  .img-badge-link:hover {
    filter: brightness(1.15);
    transform: translateY(-1px);
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
  }

  /* Image lightbox */
  #lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.9); z-index: 1000; align-items: center; justify-content: center; cursor: zoom-out; }
  #lightbox.open { display: flex; }
  #lightbox img { max-width: 90vw; max-height: 90vh; border-radius: 8px; border: 1px solid var(--border); }

  /* Typing indicator */
  .typing { display: flex; gap: 5px; padding: 14px 16px; }
  .typing span { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); animation: bounce 1.2s infinite; }
  .typing span:nth-child(2) { animation-delay: 0.2s; }
  .typing span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce { 0%,60%,100% { transform: translateY(0); } 30% { transform: translateY(-8px); } }

  /* Image captions — styled as a muted blockquote below the image */
  .bubble .img-caption { display: block; font-size: 12px; color: var(--muted); font-style: italic; margin: -6px 0 10px; padding: 4px 8px; border-left: 3px solid rgba(79,142,247,0.4); background: rgba(79,142,247,0.05); border-radius: 0 4px 4px 0; }

  /* Source badge */
  .source-link { display: inline-flex; align-items: center; gap: 4px; margin-top: 10px; padding: 5px 10px; background: rgba(79,142,247,0.1); border: 1px solid rgba(79,142,247,0.2); border-radius: 20px; font-size: 11px; color: var(--accent); text-decoration: none; }
  .source-link:hover { background: rgba(79,142,247,0.2); }

  /* Input bar */
  #input-bar { border-top: 1px solid var(--border); padding: 14px 20px; background: var(--surface); flex-shrink: 0; }
  .input-wrap { display: flex; gap: 10px; max-width: 900px; margin: 0 auto; }
  #query-input { flex: 1; background: #12141c; border: 1px solid var(--border); border-radius: 12px; padding: 11px 16px; color: var(--text); font-size: 14px; resize: none; min-height: 44px; max-height: 160px; outline: none; transition: border-color 0.2s; font-family: inherit; }
  #query-input:focus { border-color: rgba(79,142,247,0.5); }
  #query-input::placeholder { color: var(--muted); }
  #send-btn { width: 44px; height: 44px; border-radius: 12px; border: none; background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; font-size: 18px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: opacity 0.2s; flex-shrink: 0; }
  #send-btn:hover { opacity: 0.85; }
  #send-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .hint { text-align: center; font-size: 11px; color: var(--muted); margin-top: 8px; }

  /* Welcome card */
  .welcome { text-align: center; margin: auto; padding: 40px 20px; }
  .welcome .icon { font-size: 48px; margin-bottom: 16px; }
  .welcome h2 { font-size: 22px; font-weight: 600; margin-bottom: 8px; }
  .welcome p { color: var(--muted); font-size: 14px; max-width: 420px; margin: 0 auto 24px; }
  .suggestions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
  .suggestion { padding: 8px 14px; background: var(--surface); border: 1px solid var(--border); border-radius: 20px; font-size: 13px; cursor: pointer; transition: border-color 0.2s, background 0.2s; }
  .suggestion:hover { border-color: rgba(79,142,247,0.4); background: rgba(79,142,247,0.07); }

  /* Identity Selector Dropdown */
  .identity-selector-wrap {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 8px;
    background: rgba(255,255,255,0.03);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 6px 12px;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.05);
  }
  .identity-selector-wrap label {
    font-size: 10px;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.8px;
  }
  #identity-select {
    background: transparent;
    border: none;
    color: var(--text);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    outline: none;
    transition: color 0.2s;
    font-family: inherit;
    padding-right: 4px;
  }
  #identity-select option {
    background: var(--surface);
    color: var(--text);
  }
  #identity-select:hover {
    color: var(--accent);
  }
</style>
</head>
<body>
<header>
  <div class="logo">🧠</div>
  <h1>RAG Knowledge Chat</h1>
  
  <div class="identity-selector-wrap">
    <label for="identity-select">Role Identity:</label>
    <select id="identity-select" onchange="onIdentityChange()">
      <option value="guest" data-email="guest@example.com" data-groups="">Guest (Public Only)</option>
      <option value="developer" data-email="dev@company.com" data-groups="engineering">Developer (Engineering)</option>
      <option value="hr_manager" data-email="hr_manager@company.com" data-groups="hr_team">HR Manager (HR Records)</option>
      <option value="ceo" data-email="ceo@company.com" data-groups="executive_board">CEO (All Access)</option>
    </select>
  </div>

  <span id="mode-badge" class="badge">Loading...</span>
</header>

<div id="chat">
  <div class="welcome" id="welcome">
    <div class="icon">📚</div>
    <h2>Ask your corporate knowledge base</h2>
    <p>Query Confluence pages, documents, and diagrams. Source links and images are returned inline.</p>
    <div class="suggestions">
      <div class="suggestion" onclick="ask(this)">What are agent design patterns?</div>
      <div class="suggestion" onclick="ask(this)">How does A2A protocol work?</div>
      <div class="suggestion" onclick="ask(this)">Explain the Sandboxed Executor pattern</div>
      <div class="suggestion" onclick="ask(this)">What is the 429 rate limiting strategy?</div>
      <div class="suggestion" onclick="ask(this)">Show me orchestration patterns in ADK 2.0</div>
    </div>
  </div>
</div>

<div id="input-bar">
  <div class="input-wrap">
    <textarea id="query-input" placeholder="Loading knowledge base mappings…" rows="1" disabled></textarea>
    <button id="send-btn" title="Send (Enter)" disabled>&#10148;</button>
  </div>
  <div class="hint">Enter to send · Shift+Enter for new line</div>
</div>

<div id="lightbox" onclick="closeLightbox()">
  <img id="lightbox-img" src="" alt="">
</div>

<script>
const chat = document.getElementById('chat');
const input = document.getElementById('query-input');
const sendBtn = document.getElementById('send-btn');
const welcome = document.getElementById('welcome');
const badge = document.getElementById('mode-badge');
const sessionId = 'session-' + Math.random().toString(36).substring(2, 15);

// Configure marked
marked.setOptions({ breaks: true, gfm: true });

let urlMap = {};

// Normalizes image filename keys to eliminate space, case, and delimiter mismatches
function normalizeImageKey(filename) {
  if (!filename) return '';
  const idx = filename.lastIndexOf('.');
  if (idx === -1) {
    return filename.toLowerCase().replace(/[^a-z0-9]/g, '');
  }
  const name = filename.substring(0, idx).toLowerCase().replace(/[^a-z0-9]/g, '');
  const ext = filename.substring(idx).toLowerCase();
  return name + ext;
}

// Block user input interactions during configuration/mapping loading to avoid race conditions
input.disabled = true;
sendBtn.disabled = true;
input.placeholder = "Loading knowledge base mappings...";

Promise.all([
  fetch('/api/config').then(r => r.ok ? r.json() : {}),
  fetch('/api/mappings').then(r => r.ok ? r.json() : {})
]).then(([cfg, mappings]) => {
  // Update header mode badge
  if (cfg.mode) {
    const modeMap = {
      deployed_agent: ['agent', '🤖 Deployed Agent'],
      rag_corpus:     ['corpus', '🔍 RAG Corpus'],
      unconfigured:   ['unconfigured', '⚠ Unconfigured'],
    };
    const [cls, label] = modeMap[cfg.mode] || ['unconfigured', cfg.mode];
    badge.className = `badge ${cls}`;
    badge.textContent = label;
  }

  // Pre-normalize mapping keys to make lookups case/delimiter-insensitive
  const normalizedMap = {};
  for (const [key, val] of Object.entries(mappings)) {
    normalizedMap[key.toLowerCase()] = val;
    const baseKey = key.toLowerCase().endsWith('.md') ? key.substring(0, key.length - 3) : key;
    normalizedMap[baseKey.toLowerCase()] = val;
    
    // Support normalized alphanumeric keys for images/documents
    const normKey = normalizeImageKey(baseKey);
    if (normKey) {
      normalizedMap[normKey] = val;
    }
  }
  urlMap = normalizedMap;

  // Safely enable interactive elements
  input.disabled = false;
  sendBtn.disabled = false;
  input.placeholder = "Ask anything about your knowledge base…";
  input.focus();
}).catch(err => {
  console.error("[rag_chat] Failed to load configuration or mappings:", err);
  input.placeholder = "Error loading mappings. Please refresh.";
});

function resolveSourceUrl(filename) {
  if (!filename) return null;
  filename = filename.split(/[?#]/)[0];
  
  // Try normalized key lookup (fuzzy casing/spaces/delimiters)
  const normKey = normalizeImageKey(filename);
  if (urlMap[normKey]) return urlMap[normKey];
  
  // Try standard candidates
  const candidates = [
    filename.toLowerCase(),
    filename.toLowerCase() + '.md',
    filename.replace(/\.(png|jpg|jpeg|gif|svg|webp)$/i, '.$1.md').toLowerCase(),
    normKey,
    normKey + '.md'
  ];
  for (const cand of candidates) {
    if (urlMap[cand]) return urlMap[cand];
  }
  
  // Fallback scan
  const lowerFile = filename.toLowerCase();
  for (const [key, val] of Object.entries(urlMap)) {
    if (key === lowerFile || key === lowerFile + '.md' || key.endsWith(lowerFile) || lowerFile.endsWith(key)) {
      return val;
    }
  }
  
  // Confluence page ID fallback extraction
  const confMatch = filename.match(/^confluence_(\d+)_/);
  if (confMatch) {
    const pageId = confMatch[1];
    return `https://hemanthgadavajhala.atlassian.net/wiki/spaces/AD/pages/${pageId}`;
  }
  return null;
}

function ask(el) {
  input.value = el.textContent;
  sendMessage();
}

function addMessage(role, html) {
  if (welcome) welcome.remove();
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  const avatarIcon = role === 'user' ? '👤' : '🧠';
  div.innerHTML = `
    <div class="avatar">${avatarIcon}</div>
    <div class="bubble">${html}</div>`;
  // Wire up image lightbox on any img tags in agent messages
  if (role === 'agent') {
    div.querySelectorAll('img').forEach(img => {
      img.addEventListener('click', () => openLightbox(img.src));
    });
  }
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function addTyping() {
  const div = document.createElement('div');
  div.className = 'msg agent';
  div.id = 'typing';
  div.innerHTML = '<div class="avatar">🧠</div><div class="bubble"><div class="typing"><span></span><span></span><span></span></div></div>';
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function formatAnswer(md) {
  // Fix double-s typo in secure protocols (httpss:// -> https://)
  md = md.replace(/httpss:\/\//g, 'https://');

  // Clean quotes inside markdown link/image target URLs: e.g. ]('url') -> ](url)
  md = md.replace(/\]\(['"']([^\)]*?)['"']\)/g, ']($1)');
  // Clean quotes inside markdown alt/anchor texts: e.g. !['alt'] -> ![alt]
  md = md.replace(/(!?\[)['"']([^\]]*?)['"'](\])/g, '$1$2$3');

  // Clean up any legacy nested markdown link from deployed agent: [🔗 ![alt](url)](source_url) -> [🔗 alt](source_url)
  md = md.replace(/\[🔗\s*!\[([^\]]*)\]\([^)]+\)\]\(([^)]+)\)/g, (match, alt, url) => {
    let cleanAlt = alt.replace(/\.(png|jpg|jpeg|gif|svg|webp)(\.md)?$/i, '');
    cleanAlt = cleanAlt.replace(/_/g, ' ').trim();
    return `[🔗 ${cleanAlt}](${url})`;
  });

  // 1. Rewrite backticked raw image filename mentions to markdown images
  md = md.replace(/`([a-zA-Z0-9_-]+\.(?:png|jpg|jpeg|gif|svg|webp))`/g, '![$1](/api/images/$1)');

  // 2. Rewrite bracketed raw image filename mentions (like [architecture-bank-alpha.png]) to markdown images
  md = md.replace(/(?<!\!)(?<!\()\[([a-zA-Z0-9_-]+\.(?:png|jpg|jpeg|gif|svg|webp))\](?!\()/g, '![$1](/api/images/$1)');

  // 3. Rewrite raw image filename mentions like architecture-bank-alpha.png or !image-20260609.png to markdown images
  md = md.replace(/(!?)(?<![a-zA-Z0-9_\/\[\(\-\`])([a-zA-Z0-9_-]+\.(?:png|jpg|jpeg|gif|svg|webp))\b(?![a-zA-Z0-9_\)\]\-\`])/g, '![Diagram](/api/images/$2)');

  // 4. Comprehensive markdown image rewrite rule matching any ![alt](url_or_path) or [alt_with_image_ext](url_or_path)
  const imgExts = ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'];
  md = md.replace(/!?\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, url) => {
    function extractFilename(s) {
      if (!s) return null;
      try {
        const decoded = decodeURIComponent(s);
        const base = decoded.split('?')[0].split('#')[0];
        const filename = base.split('/').pop();
        const lower = filename.toLowerCase();
        for (const ext of imgExts) {
          if (lower.endsWith(ext)) {
            return filename;
          }
        }
      } catch (e) {}
      return null;
    }
    const filename = extractFilename(url) || extractFilename(alt);
    if (filename) {
      return `![${alt}](/api/images/${filename})`;
    }
    return match;
  });

  // Strip [IMAGE CAPTION: ...] and [IMAGE CONTENT: ...] completely so they are not displayed as text
  md = md.replace(/\s*\[IMAGE CAPTION:\s*(.*?)\]\s*/g, ' ');
  md = md.replace(/\s*\[IMAGE CONTENT:\s*(.*?)\]\s*/g, ' ');

  // Scan md for any proxied image references and find their source URLs
  const imageRegex = /\/api\/images\/([^)"\s]+)/g;
  let match;
  const sourceUrls = new Set();
  while ((match = imageRegex.exec(md)) !== null) {
    const filename = match[1];
    const sourceUrl = resolveSourceUrl(filename);
    if (sourceUrl) {
      sourceUrls.add(sourceUrl);
    }
  }

  // Render markdown (includes images, links, code blocks)
  let html = marked.parse(md);

  // Dynamic image wrapping & badge appending
  const tempDiv = document.createElement('div');
  tempDiv.innerHTML = html;
  const images = tempDiv.querySelectorAll('img');
  images.forEach(img => {
    const src = img.getAttribute('src');
    if (src && src.startsWith('/api/images/')) {
      const filename = src.substring('/api/images/'.length);
      const sourceUrl = resolveSourceUrl(filename);
      if (sourceUrl) {
        const wrapper = document.createElement('div');
        wrapper.className = 'img-wrapper';
        img.parentNode.insertBefore(wrapper, img);
        wrapper.appendChild(img);
        
        const badge = document.createElement('div');
        badge.className = 'img-wrapper-badge';
        
        let labelText = 'Source Document';
        let badgeColor = 'var(--accent)';
        let badgeBg = 'rgba(79,142,247,0.1)';
        let borderStyle = '1px solid rgba(79,142,247,0.2)';
        
        if (sourceUrl.includes('sharepoint.com')) {
          labelText = 'SharePoint';
          badgeColor = '#0078d4';
          badgeBg = 'rgba(0,120,212,0.1)';
          borderStyle = '1px solid rgba(0,120,212,0.2)';
          
          const decoded = decodeURIComponent(sourceUrl);
          const fileMatch = decoded.match(/file=([^&]+)/) || decoded.match(/\/([^/?]+\.[a-zA-Z0-9]+)(?:\?|$)/);
          if (fileMatch) {
            labelText = `SharePoint: ${fileMatch[1].replace(/\.md$/, '')}`;
          }
        } else if (sourceUrl.includes('atlassian.net')) {
          labelText = 'Confluence';
          badgeColor = '#0052cc';
          badgeBg = 'rgba(0,82,204,0.1)';
          borderStyle = '1px solid rgba(0,82,204,0.2)';
          
          for (const [key, val] of Object.entries(urlMap)) {
            if (val === sourceUrl) {
              labelText = `Confluence: ${key.replace(/_/g, ' ').replace(/\.md$/, '')}`;
              break;
            }
          }
        }
        
        badge.innerHTML = `
          <span style="color: var(--muted); font-size: 11px;">Image Source:</span>
          <a href="${sourceUrl}" target="_blank" style="
            color: ${badgeColor}; 
            background: ${badgeBg}; 
            border: ${borderStyle};
            padding: 3px 10px; 
            border-radius: 12px; 
            text-decoration: none; 
            font-weight: 500;
            font-size: 11px;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            transition: all 0.2s ease;
          " class="img-badge-link">
            🔗 ${escapeHtml(labelText)}
          </a>
        `;
        wrapper.appendChild(badge);
      }
    }
  });
  html = tempDiv.innerHTML;

  // If we collected any unique resolved URLs, append them at the bottom
  if (sourceUrls.size > 0) {
    let sourceBadgesHtml = '<div style="margin-top: 15px; border-top: 1px solid var(--border); padding-top: 10px;">';
    sourceBadgesHtml += '<strong style="display: block; margin-bottom: 6px; font-size: 12px; color: var(--muted);">Retrieved Source Documents:</strong>';
    let addedAny = false;
    const seenUrlsInBottom = new Set();
    for (const url of sourceUrls) {
      if (seenUrlsInBottom.has(url)) {
        continue;
      }
      seenUrlsInBottom.add(url);
      
      let title = 'Source Page';
      if (url.includes('sharepoint.com')) {
        title = 'SharePoint Document';
        const decoded = decodeURIComponent(url);
        const nameMatch = decoded.match(/file=([^&]+)/) || decoded.match(/\/([^/?]+\.[a-zA-Z0-9]+)(?:\?|$)/);
        if (nameMatch) {
          title = `SharePoint: ${nameMatch[1].replace(/\.md$/, '')}`;
        }
      } else if (url.includes('atlassian.net')) {
        title = 'Confluence Page';
        for (const [key, val] of Object.entries(urlMap)) {
          if (val === url) {
            title = `Confluence: ${key.replace(/_/g, ' ').replace(/\.md$/, '')}`;
            break;
          }
        }
      }
      sourceBadgesHtml += `<a href="${url}" target="_blank" class="source-link" style="margin-right: 8px; margin-bottom: 8px;">🔗 ${escapeHtml(title)}</a>`;
      addedAny = true;
    }
    sourceBadgesHtml += '</div>';
    if (addedAny) {
      html += sourceBadgesHtml;
    }
  }

  return html;
}

async function sendMessage() {
  const q = input.value.trim();
  if (!q || input.disabled) return;
  input.value = '';
  input.style.height = '';
  sendBtn.disabled = true;

  addMessage('user', escapeHtml(q));
  const typingEl = addTyping();

  try {
    // Retrieve identity parameters from dropdown selection
    const idSelect = document.getElementById('identity-select');
    const selectedOpt = idSelect.options[idSelect.selectedIndex];
    const email = selectedOpt.getAttribute('data-email');
    const groups = selectedOpt.getAttribute('data-groups');

    // Use SSE streaming endpoint with identity propagation
    const url = '/api/stream?query=' + encodeURIComponent(q) + 
                '&session_id=' + sessionId + 
                '&user_email=' + encodeURIComponent(email) + 
                '&user_groups=' + encodeURIComponent(groups);
                
    const evtSource = new EventSource(url);
    let accumulated = '';
    let agentBubble = null;

    evtSource.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.done) {
        evtSource.close();
        sendBtn.disabled = false;
        return;
      }
      if (typingEl.parentNode) typingEl.remove();
      accumulated += (data.token || '');

      if (!agentBubble) {
        agentBubble = addMessage('agent', formatAnswer(accumulated));
      } else {
        const bubble = agentBubble.querySelector('.bubble');
        bubble.innerHTML = formatAnswer(accumulated);
        // Re-wire lightbox on new images
        bubble.querySelectorAll('img').forEach(img => {
          if (!img._lb) { img._lb = true; img.addEventListener('click', () => openLightbox(img.src)); }
        });
      }
      chat.scrollTop = chat.scrollHeight;
    };

    evtSource.onerror = () => {
      evtSource.close();
      if (typingEl.parentNode) typingEl.remove();
      if (!agentBubble) addMessage('agent', '<em>Connection error. Please try again.</em>');
      sendBtn.disabled = false;
    };
  } catch(err) {
    typingEl.remove();
    addMessage('agent', `<em>Error: ${escapeHtml(String(err))}</em>`);
    sendBtn.disabled = false;
  }
}

function onIdentityChange() {
  const idSelect = document.getElementById('identity-select');
  const selectedOpt = idSelect.options[idSelect.selectedIndex];
  const label = selectedOpt.text;
  const email = selectedOpt.getAttribute('data-email');
  
  addSystemNotification(`Switched role session to <strong>${escapeHtml(label)}</strong> (${escapeHtml(email)})`);
}

function addSystemNotification(html) {
  const div = document.createElement('div');
  div.className = 'system-notification';
  div.style.alignSelf = 'center';
  div.style.margin = '12px auto';
  div.style.padding = '8px 18px';
  div.style.borderRadius = '30px';
  div.style.background = 'rgba(79,142,247,0.06)';
  div.style.border = '1px solid rgba(79,142,247,0.18)';
  div.style.fontSize = '12px';
  div.style.color = 'var(--muted)';
  div.style.textAlign = 'center';
  div.style.maxWidth = '650px';
  div.style.boxShadow = '0 2px 8px rgba(0,0,0,0.1)';
  div.innerHTML = `🛡 ${html}`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function openLightbox(src) {
  document.getElementById('lightbox-img').src = src;
  document.getElementById('lightbox').classList.add('open');
}
function closeLightbox() {
  document.getElementById('lightbox').classList.remove('open');
}

// Input auto-resize + send on Enter
input.addEventListener('input', () => {
  input.style.height = '';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
sendBtn.addEventListener('click', sendMessage);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(
        content=_HTML,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("RAG_CHAT_PORT", "8020"))
    print(f"[rag_chat] Starting on http://localhost:{port}")
    print(f"[rag_chat] Mode: {'deployed_agent' if RESOURCE_NAME else ('rag_corpus' if CORPUS_ID else 'unconfigured')}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
