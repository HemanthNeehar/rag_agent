import os
import re
import shutil
import subprocess
import urllib.request
import httpx
import json
import concurrent.futures
import threading
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load env from workspace root
workspace_dir = Path(__file__).parent.parent
load_dotenv(workspace_dir / ".env")

from rag_agent.ingest import get_mock_confluence_data, get_mock_sharepoint_data

# Thread-safe semaphore to restrict concurrent Gemini API calls and prevent rate limiting (429 errors)
gemini_semaphore = threading.Semaphore(3)

# Thread-safe lock for catalog & map file writes and GCS uploads
state_lock = threading.Lock()
last_gcs_upload_time = 0.0
confluence_failures = []

# Global permissions map to match GCS files with dynamic enterprise permissions
gcs_permissions_map = {}

def fetch_page_restrictions(base_api: str, page_id: str, auth: tuple | None, headers: dict | None) -> dict:
    """Fetches read restrictions for a Confluence page.
    Returns a dict with: {'restricted': bool, 'allowed_users': list[str], 'allowed_groups': list[str]}"""
    try:
        url = f"{base_api}/content/{page_id}/restriction/byOperation"
        resp = httpx.get(url, auth=auth, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            read_restrictions = data.get("read", {})
            restrictions_data = read_restrictions.get("restrictions", {})
            
            allowed_users = []
            allowed_groups = []
            
            users = restrictions_data.get("user", {}).get("results", [])
            for u in users:
                email = u.get("email")
                if email:
                    allowed_users.append(email.lower())
                accountId = u.get("accountId")
                if accountId:
                    allowed_users.append(accountId)
                    
            groups = restrictions_data.get("group", {}).get("results", [])
            for g in groups:
                name = g.get("name")
                if name:
                    allowed_groups.append(name.lower())
            
            # Restricted if actual entries are configured on this page
            restricted = len(allowed_users) > 0 or len(allowed_groups) > 0
            return {
                "restricted": restricted,
                "allowed_users": allowed_users,
                "allowed_groups": allowed_groups
            }
    except Exception as e:
        print(f"     [Warning] Failed fetching restrictions for page {page_id}: {e}")
    return {"restricted": False, "allowed_users": [], "allowed_groups": []}

def save_and_upload_markdown_doc(doc: dict, temp_dir: Path, bucket_name: str) -> str:
    """Helper to enrich, write, and upload a single markdown doc to GCS. Returns the cleaned filename."""
    filename = clean_filename(doc["title"])
    filepath = temp_dir / filename
    content = doc["content"]

    # Replace any local image paths with public GCS URLs so the RAG engine
    # stores and returns fully qualified image links the agent can cite in UI.
    content = re.sub(
        r'!\[([^\]]*)\]\(images/([^)]+)\)',
        lambda m: f"![{m.group(1)}](https://storage.googleapis.com/{bucket_name}/images/{m.group(2)})",
        content,
    )

    restricted_str = "True" if doc.get("restricted") else "False"
    allowed_groups_str = ",".join(doc.get("allowed_groups", []))
    allowed_users_str = ",".join(doc.get("allowed_users", []))

    enriched_content = (
        f"source_url: {doc['url']}\n"
        f"title: {doc['title']}\n"
        f"source_system: {doc['source']}\n"
        f"restricted: {restricted_str}\n"
        f"allowed_groups: {allowed_groups_str}\n"
        f"allowed_users: {allowed_users_str}\n\n"
        f"{content}\n\n"
        f"---\n"
        f"doc_id: {doc['id']}\n"
        f"source_url: {doc['url']}\n"
    )

    try:
        filepath.write_text(enriched_content, encoding="utf-8")
        if bucket_name and bucket_name != "YOUR_BUCKET_NAME":
            if upload_file_to_gcs(filepath, f"gs://{bucket_name}/{filename}"):
                print(f"     -> [GCS] Uploaded markdown: {filename}")
    except Exception as e:
        print(f"     [Warning] Failed uploading markdown {filename}: {e}")
    finally:
        try:
            if filepath.exists():
                filepath.unlink()
        except Exception as del_err:
            print(f"     [Warning] Failed deleting local markdown {filename}: {del_err}")
    return filename

def save_state_incrementally(catalog: dict, gcs_confluence_map: dict, bucket_name: str, gcs_permissions_map: dict = None):
    """Saves the current catalog and URL map locally and uploads to GCS if throttled limit permits."""
    global last_gcs_upload_time
    import time
    catalog_filepath = Path(__file__).parent / "ingestion_catalog.json"
    map_filepath = Path(__file__).parent / "gcs_confluence_map.json"
    perm_filepath = Path(__file__).parent / "gcs_confluence_permissions_map.json"
    try:
        with open(catalog_filepath, "w", encoding="utf-8") as f:
            json.dump(catalog, f, indent=2)
        with open(map_filepath, "w", encoding="utf-8") as f:
            json.dump(gcs_confluence_map, f, indent=2)
        if gcs_permissions_map is not None:
            with open(perm_filepath, "w", encoding="utf-8") as f:
                json.dump(gcs_permissions_map, f, indent=2)
        
        current_time = time.time()
        # Throttle uploads to once every 30 seconds
        if current_time - last_gcs_upload_time >= 30.0:
            upload_state_to_gcs(bucket_name, catalog_filepath, map_filepath, perm_filepath if gcs_permissions_map is not None else None)
            last_gcs_upload_time = current_time
            print(f"     -> [GCS] Intermittent progress uploaded successfully.")
    except Exception as state_err:
        print(f"     [Warning] Failed saving intermittent progress: {state_err}")


def clean_filename(title: str) -> str:
    """Removes special characters to make a safe filename."""
    name = re.sub(r'[\\/*?:"<>|\'`]', "", title)  # includes apostrophe/backtick
    name = name.replace(" ", "_")
    if not name.endswith(".md") and not name.endswith(".html"):
        name += ".md"
    return name

_gcs_client = None

def get_gcs_client():
    global _gcs_client
    if _gcs_client is None:
        try:
            from google.cloud import storage
            _gcs_client = storage.Client()
        except Exception as e:
            print(f"  [Warning] Failed to initialize in-process GCS client: {e}. Will fall back to CLI.")
    return _gcs_client

def upload_file_to_gcs(local_path: str, gcs_uri: str) -> bool:
    """Uploads a file to GCS using in-process client if available, falling back to CLI."""
    import subprocess
    if not gcs_uri.startswith("gs://"):
        print(f"  [Error] Invalid GCS URI: {gcs_uri}")
        return False
        
    parts = gcs_uri[5:].split("/", 1)
    bucket_name = parts[0]
    blob_name = parts[1] if len(parts) > 1 else ""
    
    # Try in-process first
    client = get_gcs_client()
    if client:
        try:
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.upload_from_filename(str(local_path))
            return True
        except Exception as e:
            print(f"  [Warning] In-process upload to {gcs_uri} failed: {e}. Falling back to CLI.")
            
    # Fallback to CLI
    try:
        cmd = f'gcloud storage cp "{local_path}" "{gcs_uri}"'
        subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"  [Error] CLI GCS upload to {gcs_uri} failed: {e}")
        return False

def download_file_from_gcs(gcs_uri: str, local_path: str, silent=True) -> bool:
    """Downloads a file from GCS using in-process client if available, falling back to CLI."""
    import subprocess
    if not gcs_uri.startswith("gs://"):
        print(f"  [Error] Invalid GCS URI: {gcs_uri}")
        return False
        
    parts = gcs_uri[5:].split("/", 1)
    bucket_name = parts[0]
    blob_name = parts[1] if len(parts) > 1 else ""
    
    # Try in-process first
    client = get_gcs_client()
    if client:
        try:
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            if blob.exists():
                blob.download_to_filename(str(local_path))
                return True
            else:
                if not silent:
                    print(f"  [Info] GCS blob does not exist: {gcs_uri}")
                return False
        except Exception as e:
            if not silent:
                print(f"  [Warning] In-process download from {gcs_uri} failed: {e}. Falling back to CLI.")
                
    # Fallback to CLI
    try:
        cmd = f'gcloud storage cp "{gcs_uri}" "{local_path}"'
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from pathlib import Path
        return Path(local_path).exists()
    except Exception as e:
        if not silent:
            print(f"  [Error] CLI GCS download from {gcs_uri} failed: {e}")
        return False

def delete_file_from_gcs(gcs_uri: str) -> bool:
    """Deletes a file from GCS using in-process client if available, falling back to CLI."""
    import subprocess
    if not gcs_uri.startswith("gs://"):
        print(f"  [Error] Invalid GCS URI: {gcs_uri}")
        return False
        
    parts = gcs_uri[5:].split("/", 1)
    bucket_name = parts[0]
    blob_name = parts[1] if len(parts) > 1 else ""
    
    # Try in-process first
    client = get_gcs_client()
    if client:
        try:
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            if blob.exists():
                blob.delete()
                return True
            return True
        except Exception as e:
            print(f"  [Warning] In-process deletion of {gcs_uri} failed: {e}. Falling back to CLI.")
            
    # Fallback to CLI
    try:
        cmd = f'gcloud storage rm "{gcs_uri}"'
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"  [Error] CLI GCS deletion of {gcs_uri} failed: {e}")
        return False

def get_gemini_client() -> genai.Client:
    """Initialize the Google GenAI SDK client with a 60-second timeout to prevent hangs."""
    # Ensure active service account or credentials from env are propagated
    sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH")
    if sa_path:
        if not os.path.isabs(sa_path):
            sa_path = str(workspace_dir / sa_path)
        if os.path.exists(sa_path):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_path
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
            
    # Override location to 'us' for Gemini 3.5 models on Vertex AI (they are not supported on 'us-central1' yet)
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    if model_name == "gemini-3.5-flash":
        os.environ["GOOGLE_CLOUD_LOCATION"] = "us"
        os.environ["GCP_LOCATION"] = "us"
            
    return genai.Client(
        http_options=types.HttpOptions(timeout=60_000)
    )

def describe_image_with_gemini(client: genai.Client, image_bytes: bytes, mime_type: str = "image/png") -> str:
    """Sends image bytes to Gemini to generate a detailed textual description with exponential backoff retries on 429."""
    import time
    import random
    
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    prompt = (
        "Generate a detailed textual description of this diagram, including all labels, "
        "flowcharts, structural components, and systems shown."
    )
    
    max_retries = 5
    base_delay = 2.0
    
    for attempt in range(max_retries):
        try:
            with gemini_semaphore:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                        prompt
                    ]
                )
            return response.text or "No description generated."
        except Exception as e:
            err_msg = str(e)
            is_429 = "429" in err_msg or "resource_exhausted" in err_msg.lower() or "rate" in err_msg.lower()
            
            if is_429 and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0.1, 1.0)
                print(f"    [Warning] Gemini rate limited (429) on attempt {attempt + 1}. Retrying in {delay:.2f}s... Error: {e}")
                time.sleep(delay)
            else:
                print(f"    [Warning] Gemini captioning failed: {e}")
                if not is_429:
                    break
                    
    return "Image description generation failed."

def _strip_html(html_content: str) -> str:
    """Converts Confluence storage-format HTML/XML to plain readable text."""
    # Remove script/style blocks
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    # Replace Confluence structured macros with a placeholder
    text = re.sub(r'<ac:structured-macro[^>]*>.*?</ac:structured-macro>', '\n[CODE BLOCK]\n', text, flags=re.DOTALL | re.IGNORECASE)
    # Replace block elements with newlines
    text = re.sub(r'</?(p|div|br|h[1-6]|li|tr|td|th|ac:task-body)[^>]*>', '\n', text, flags=re.IGNORECASE)
    # Strip all remaining XML/HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Decode common HTML entities
    text = (text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>')
                .replace('&amp;', '&').replace('&quot;', '"').replace('&#39;', "'"))
def generate_llm_vsdx_summary(shape_texts_str: str, file_name: str, image_bytes: bytes | None = None, mime_type: str = "image/png") -> str:
    """Generates a high-fidelity architectural flow narrative for Visio/Draw.io diagrams using Gemini 2.5 Pro Multimodal Vision."""
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("RAG_PROJECT_ID") or os.getenv("GCP_PROJECT") or "agent-ops-494011"
    location = os.getenv("GOOGLE_CLOUD_LOCATION") or os.getenv("RAG_LOCATION") or "us-central1"
    vision_model = os.getenv("GEMINI_MODEL_PRO", "gemini-2.5-pro")

    has_img_str = " + Thumbnail Image Canvas" if image_bytes else ""
    print(f"      ➜ [Gemini Pro Vision] Summarizing diagram '{file_name}' using {vision_model}{has_img_str}...")

    prompt = f"""You are an Enterprise Solutions Architect analyzing a High-Level Design (HLD/LLD) diagram: {file_name}.

CRITICAL VERBATIM EXTRACTION REQUIREMENTS:
1. VERBATIM TERMS & ACRONYMS: Extract and retain ALL visible text, shape headers, field labels, acronyms, and callout terms VERBATIM. Do NOT omit, summarize away, or paraphrase technical terms, IDs, or search inputs (e.g., 'CSNOs', 'find by CSNOs', 'BMUI', 'HSI', 'LCI', API endpoints, table names, button text).
2. VISUAL & SPATIAL FLOW: Below are the extracted shape text labels pre-sorted in visual spatial order (Top->Bottom, Left->Right):

{shape_texts_str}

3. COMPREHENSIVE ARCHITECTURAL NARRATIVE:
   - Identify all system components, microservices, databases, UI forms, and external gateways.
   - Describe the step-by-step workflow sequences and API call sequences.
   - Document conditional logic, decision branches ('If success', 'If error', 'Update LCI', etc.), and error handling flows.
   - Ensure every domain acronym, UI search label, and subsystem tag mentioned in the diagram or image is explicitly documented.

Format clearly in structured Markdown with headers and bullet points."""

    # Tier 1: google-genai SDK
    try:
        from google import genai
        from google.genai import types
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            client = genai.Client(api_key=api_key)
        else:
            client = genai.Client(vertexai=True, project=project_id, location=location)

        contents = []
        if image_bytes:
            contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
        contents.append(prompt)

        response = client.models.generate_content(model=vision_model, contents=contents)
        if response and response.text:
            print(f"      ✔ [Gemini Pro Vision] Generated {len(response.text)} chars architectural narrative for '{file_name}'.")
            return response.text
    except Exception as e1:
        print(f"      [Info] google.genai Client failed for '{file_name}' ({e1}). Attempting vertexai fallback...")

    # Tier 2: Official vertexai SDK (vertexai.generative_models)
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, Part
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel(vision_model)
        
        contents = []
        if image_bytes:
            contents.append(Part.from_data(data=image_bytes, mime_type=mime_type))
        contents.append(prompt)

        response = model.generate_content(contents)
        if response and response.text:
            print(f"      ✔ [Gemini Pro Vision] Fallback generated {len(response.text)} chars architectural narrative for '{file_name}'.")
            return response.text
    except Exception as e2:
        print(f"      [Warning] Both Gemini Vision SDK attempts failed for '{file_name}': {e2}. Diagram will contain spatial labels only.")

    return ""


def _parse_vsdx_locally(file_path: Path) -> str:
    """Extracts text labels from Visio (.vsdx) files using spatial (PinX, PinY) coordinate sorting,
    extracts embedded preview images (docProps/thumbnail.png / visio/media/), and calls Gemini Vision."""
    import zipfile
    import xml.etree.ElementTree as ET
    spatial_texts = []
    extracted_image_bytes = None
    image_mime_type = "image/png"

    try:
        with zipfile.ZipFile(file_path) as vsdx:
            # 1. Extract thumbnail / preview image if available
            for name in vsdx.namelist():
                if name.lower().endswith((".png", ".jpg", ".jpeg")):
                    if "thumbnail" in name.lower() or "visio/media/" in name.lower() or "media" in name.lower():
                        extracted_image_bytes = vsdx.read(name)
                        if name.lower().endswith(".jpg") or name.lower().endswith(".jpeg"):
                            image_mime_type = "image/jpeg"
                        break

            # 2. Extract shape text with spatial coordinates (PinX, PinY)
            xml_files = [name for name in vsdx.namelist() if name.startswith('visio/pages/page') and name.endswith('.xml')]
            for page_file in sorted(xml_files):
                page_data = vsdx.read(page_file)
                clean_xml = re.sub(r'xmlns(:\w+)?="[^"]+"', '', page_data.decode('utf-8', errors='ignore'))
                root = ET.fromstring(clean_xml)
                
                shapes = []
                for shape in root.findall('.//Shape'):
                    pin_x = 0.0
                    pin_y = 0.0
                    for cell in shape.findall('.//Cell'):
                        n = cell.get('N', '')
                        v = cell.get('V', '0')
                        if n == 'PinX':
                            try: pin_x = float(v)
                            except: pass
                        elif n == 'PinY':
                            try: pin_y = float(v)
                            except: pass

                    text_elems = shape.findall('.//Text')
                    elem_texts = []
                    for t in text_elems:
                        txt = "".join(t.itertext()).strip()
                        if txt:
                            elem_texts.append(txt)

                    # Fallback for cell values or custom shape property labels if Text element was empty
                    if not elem_texts:
                        for cell_val in shape.findall(".//Cell[@N='Value']"):
                            v_val = cell_val.get('V', '').strip()
                            if v_val and len(v_val) > 1 and not v_val.replace('.', '').isdigit():
                                elem_texts.append(v_val)
                    
                    if elem_texts:
                        full_txt = " | ".join(elem_texts)
                        shapes.append((pin_y, pin_x, full_txt))
                
                # Sort shapes spatially: Top-to-Bottom (-pin_y), Left-to-Right (pin_x)
                shapes.sort(key=lambda s: (-s[0], s[1]))
                
                page_label_lines = [s[2] for s in shapes]
                if page_label_lines:
                    spatial_texts.append(f"### Visio Page ({os.path.basename(page_file)} spatially sorted Top->Bottom, Left->Right):\n" + "\n".join(page_label_lines))

    except Exception as e:
        print(f"      [Warning] Spatial VSDX parse failed: {e}")
        return f"Local Visio parsing failed: {e}"

    raw_shape_content = "\n\n".join(spatial_texts)
    
    llm_summary = generate_llm_vsdx_summary(
        shape_texts_str=raw_shape_content,
        file_name=file_path.name,
        image_bytes=extracted_image_bytes,
        mime_type=image_mime_type
    )

    if llm_summary:
        return f"## Architectural Workflow Narrative (Gemini Multimodal Generated)\n{llm_summary}\n\n## Spatially-Ordered Visio Elements\n{raw_shape_content}"
    return raw_shape_content


def _parse_drawio_locally(file_path: Path) -> str:
    """Extracts text and labels from Draw.io XML files and generates a Gemini architectural flow summary."""
    import xml.etree.ElementTree as ET
    import base64, zlib, urllib.parse
    texts = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore").strip()
        if not content:
            return "Empty Draw.io file."
            
        root = ET.fromstring(content)
        diagram_nodes = root.findall('.//diagram')
        if diagram_nodes:
            for idx, diag in enumerate(diagram_nodes):
                diag_text = diag.text
                if diag_text:
                    try:
                        decoded_bytes = base64.b64decode(diag_text.strip())
                        decompressed_bytes = zlib.decompress(decoded_bytes, -zlib.MAX_WBITS)
                        decompressed_text = urllib.parse.unquote(decompressed_bytes.decode('utf-8'))
                        diag_root = ET.fromstring(decompressed_text)
                        for cell in diag_root.findall('.//mxCell'):
                            value = cell.get('value')
                            if value:
                                clean_val = re.sub(r'<[^>]+>', ' ', value).strip()
                                clean_val = urllib.parse.unquote(clean_val)
                                if clean_val:
                                    texts.append(clean_val)
                    except Exception as decompress_err:
                        pass
        
        for cell in root.findall('.//mxCell'):
            value = cell.get('value')
            if value:
                clean_val = re.sub(r'<[^>]+>', ' ', value).strip()
                if clean_val and clean_val not in texts:
                    texts.append(clean_val)
                    
    except Exception as e:
        print(f"      [Warning] Local Draw.io parse failed: {e}")
        return f"Local Draw.io parsing failed: {e}"
        
    if not texts:
        return "Empty Draw.io diagram or no text labels found."
    
    raw_drawio_content = "### Draw.io Diagram Labels:\n" + "\n".join(texts)
    llm_summary = generate_llm_vsdx_summary(raw_drawio_content, file_path.name)
    
    if llm_summary:
        return f"## Architectural Workflow Narrative (Gemini Generated)\n{llm_summary}\n\n## Raw Draw.io Page Elements\n{raw_drawio_content}"
    return raw_drawio_content


def _convert_html_tables_to_markdown(html_content: str) -> str:
    """Parses standard tables in html_content using BeautifulSoup,
    converts them to Markdown tables, and replaces them inline."""
    if not html_content or "<table" not in html_content.lower():
        return html_content

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, "html.parser")
        tables = soup.find_all("table")
        for table in tables:
            markdown_table = []
            rows = table.find_all("tr")
            if not rows:
                continue
            
            # Determine maximum columns
            max_cols = 0
            for row in rows:
                cols = row.find_all(["td", "th"])
                max_cols = max(max_cols, len(cols))
            
            if max_cols == 0:
                continue

            header_seen = False
            for row in rows:
                cols = row.find_all(["td", "th"])
                row_cells = []
                is_header_row = False
                
                for cell in cols:
                    if cell.name == "th":
                        is_header_row = True
                    # Clean up cell text. Use spaces for inner block tags to avoid breaking markdown rows
                    cell_text = cell.get_text(separator=" ", strip=True)
                    # Escape pipe characters and remove newlines
                    cell_text = cell_text.replace("|", "\\|").replace("\n", " ").replace("\r", "")
                    row_cells.append(cell_text)
                
                # Pad cells
                while len(row_cells) < max_cols:
                    row_cells.append("")
                
                row_str = "| " + " | ".join(row_cells) + " |"
                markdown_table.append(row_str)
                
                if is_header_row and not header_seen:
                    separator = "| " + " | ".join(["---"] * max_cols) + " |"
                    markdown_table.append(separator)
                    header_seen = True
            
            if not header_seen and len(markdown_table) > 0:
                separator = "| " + " | ".join(["---"] * max_cols) + " |"
                markdown_table.insert(1, separator)

            table_md = "\n\n" + "\n".join(markdown_table) + "\n\n"
            table.replace_with(table_md)
        
        return str(soup)
    except Exception as e:
        print(f"    [Warning] Failed to convert HTML tables: {e}")
        return html_content


def download_attachment(conf_url: str, auth: tuple | None, page_id: str, att: dict, temp_dir: Path, headers: dict | None = None) -> Path | None:
    """Downloads an attachment from Confluence and saves it locally in temp_dir using memory-efficient chunked streaming.
    Returns the Path to the local file, or None if download failed."""
    fname = att.get("title", "")
    if not fname:
        return None
        
    try:
        resp_base = att.get("_links", {}).get("base", f"{conf_url.rstrip('/')}/wiki")
        download_rel = att.get("_links", {}).get("download", "")
        
        download_urls = []
        # 1. REST API /download endpoint
        download_urls.append(f"{resp_base.rstrip('/')}/rest/api/content/{page_id}/child/attachment/{att['id']}/download")
        # 2. Direct relative download path with query parameters
        if download_rel:
            download_urls.append(f"{resp_base.rstrip('/')}{download_rel}")
            # 3. Direct relative download path without query parameters
            download_urls.append(f"{resp_base.rstrip('/')}{download_rel}".split("?")[0])
            
        # De-duplicate URLs to avoid duplicate network requests
        download_urls = list(dict.fromkeys(download_urls))
            
        max_size_mb = int(os.getenv("CONFLUENCE_MAX_FILE_SIZE_MB", "500"))
        max_size_bytes = max_size_mb * 1024 * 1024
        
        safe_name = re.sub(r'[\\/*?:"<>|\'`]', "", fname).replace(" ", "_")
        local_path = temp_dir / safe_name
        
        download_success = False
        last_error = None
        
        for d_url in download_urls:
            try:
                # Use stream to download in chunks and check size on the fly
                with httpx.stream("GET", d_url, auth=auth, headers=headers, follow_redirects=True, timeout=120) as r:
                    if r.status_code != 200:
                        last_error = f"HTTP {r.status_code}"
                        if r.status_code == 404:
                            # 404 Not Found means the file does not exist on Confluence. Stop attempting other URLs.
                            break
                        continue
                    
                    # Check Content-Length header first if available
                    content_length = r.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > max_size_bytes:
                                print(f"     [Warning] Skipping large Confluence attachment {fname} ({int(content_length) / (1024*1024):.2f} MB) via Content-Length header - Max limit: {max_size_mb} MB")
                                return None
                        except ValueError:
                            pass
                    
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    bytes_downloaded = 0
                    with open(local_path, "wb") as f:
                        for chunk in r.iter_bytes(chunk_size=8192):
                            bytes_downloaded += len(chunk)
                            if bytes_downloaded > max_size_bytes:
                                print(f"     [Warning] Skipping large Confluence attachment {fname} (> {max_size_mb} MB) - Max limit reached during download. Aborting.")
                                f.close()
                                if local_path.exists():
                                    local_path.unlink()
                                return None
                            f.write(chunk)
                    
                    if local_path.exists() and local_path.stat().st_size > 0:
                        download_success = True
                        break
            except Exception as ex:
                last_error = str(ex)
                if local_path.exists():
                    try:
                        local_path.unlink()
                    except Exception:
                        pass
                        
        if not download_success:
            print(f"     [Warning] Attachment download failed for {fname}: {last_error}")
            return None
            
        return local_path
    except Exception as e:
        print(f"     [Warning] Failed downloading attachment {fname}: {e}")
        return None



def _parse_docx_locally(file_path: Path) -> str:
    """Extracts text and tables from DOCX locally using python-docx (primary) or zipfile XML parsing (fallback)."""
    # 1. Try python-docx if installed
    try:
        import docx
        doc = docx.Document(file_path)
        texts = []
        for p in doc.paragraphs:
            if p.text.strip():
                texts.append(p.text.strip())
        for table in doc.tables:
            table_rows = []
            for row in table.rows:
                row_cells = [cell.text.strip().replace("|", "\\|").replace("\n", " ") for cell in row.cells]
                if any(row_cells):
                    table_rows.append("| " + " | ".join(row_cells) + " |")
            if table_rows:
                if len(table_rows) > 0:
                    num_cols = len(table_rows[0].split("|")) - 2
                    separator = "| " + " | ".join(["---"] * num_cols) + " |"
                    table_rows.insert(1, separator)
                texts.append("\n" + "\n".join(table_rows) + "\n")
        return "\n\n".join(texts)
    except Exception as docx_err:
        print(f"      [Info] python-docx fallback parse triggered or failed: {docx_err}. Using zipfile fallback.")

    # 2. Pure zipfile XML fallback (case-insensitive file names)
    import zipfile
    import xml.etree.ElementTree as ET
    
    texts = []
    try:
        with zipfile.ZipFile(file_path) as docx:
            doc_name = next((n for n in docx.namelist() if n.lower() == 'word/document.xml'), 'word/document.xml')
            xml_content = docx.read(doc_name)
            root = ET.fromstring(xml_content)
            
            namespaces = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            
            for elem in root.iter():
                if elem.tag.endswith('}p'):  # paragraph
                    p_text = "".join(node.text for node in elem.iter() if node.tag.endswith('}t') and node.text)
                    if p_text:
                        texts.append(p_text)
                elif elem.tag.endswith('}tbl'): # table
                    table_rows = []
                    for row in elem.findall('.//w:tr', namespaces):
                        row_cells = []
                        for cell in row.findall('.//w:tc', namespaces):
                            cell_text = "".join(node.text for node in cell.iter() if node.tag.endswith('}t') and node.text)
                            row_cells.append(cell_text.strip().replace("|", "\\|").replace("\n", " "))
                        if row_cells:
                            table_rows.append("| " + " | ".join(row_cells) + " |")
                    if table_rows:
                        if len(table_rows) > 0:
                            num_cols = len(table_rows[0].split("|")) - 2
                            separator = "| " + " | ".join(["---"] * num_cols) + " |"
                            table_rows.insert(1, separator)
                        texts.append("\n" + "\n".join(table_rows) + "\n")
    except Exception as e:
        print(f"      [Warning] Local DOCX parse failed: {e}")
        return f"Local DOCX parsing failed: {e}"
    return "\n\n".join(texts)


def _parse_pptx_locally(file_path: Path) -> str:
    """Extracts text from PPTX locally using python-pptx (primary) or zipfile XML parsing (fallback)."""
    # 1. Try python-pptx if installed
    try:
        from pptx import Presentation
        prs = Presentation(file_path)
        texts = []
        for i, slide in enumerate(prs.slides, start=1):
            texts.append(f"## Slide {i}")
            slide_texts = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    slide_texts.append(shape.text.strip())
                if shape.has_table:
                    for row in shape.table.rows:
                        row_cells = [cell.text.strip().replace("|", "\\|").replace("\n", " ") for cell in row.cells]
                        if any(row_cells):
                            slide_texts.append("| " + " | ".join(row_cells) + " |")
            if slide_texts:
                texts.append("\n".join(slide_texts))
        return "\n\n".join(texts)
    except Exception as pptx_err:
        print(f"      [Info] python-pptx fallback parse triggered or failed: {pptx_err}. Using zipfile fallback.")

    # 2. Pure zipfile XML fallback (case-insensitive file names)
    import zipfile
    import xml.etree.ElementTree as ET
    
    texts = []
    try:
        with zipfile.ZipFile(file_path) as pptx:
            slide_files = sorted(
                [name for name in pptx.namelist() if name.lower().startswith('ppt/slides/slide') and name.endswith('.xml')],
                key=lambda x: int(re.search(r'\d+', x).group()) if re.search(r'\d+', x) else 0
            )
            
            for i, slide_name in enumerate(slide_files, start=1):
                texts.append(f"## Slide {i}")
                slide_content = pptx.read(slide_name)
                root = ET.fromstring(slide_content)
                
                slide_texts = []
                for elem in root.iter():
                    if elem.tag.endswith('}t') and elem.text:
                        slide_texts.append(elem.text.strip())
                
                if slide_texts:
                    texts.append("\n".join(slide_texts))
    except Exception as e:
        print(f"      [Warning] Local PPTX parse failed: {e}")
        return f"Local PPTX parsing failed: {e}"
    return "\n\n".join(texts)


def _parse_pdf_locally(file_path: Path) -> str:
    """Extracts text from PDF locally using pypdf."""
    from pypdf import PdfReader
    texts = []
    try:
        reader = PdfReader(file_path)
        for page_num, page in enumerate(reader.pages, start=1):
            texts.append(f"## Page {page_num}")
            text = page.extract_text()
            if text:
                texts.append(text)
    except Exception as e:
        print(f"      [Warning] Local PDF parse failed: {e}")
        return f"Local PDF parsing failed: {e}"
    return "\n\n".join(texts)


def extract_images_from_attachment(file_path: Path, temp_images_dir: Path) -> list:
    """Extracts embedded images from PDF, DOCX, or PPTX and saves them.
    Returns a list of tuples: (safe_image_name, local_path)"""
    extracted = []
    ext = file_path.suffix.lower()
    
    if ext in (".docx", ".pptx"):
        import zipfile
        media_dir = "word/media/" if ext == ".docx" else "ppt/media/"
        try:
            with zipfile.ZipFile(file_path) as z:
                media_files = [name for name in z.namelist() if name.startswith(media_dir)]
                for name in media_files:
                    try:
                        # Skip if it is not a supported raster image extension to prevent Gemini 400 bad image errors
                        original_fname = os.path.basename(name)
                        img_ext = os.path.splitext(original_fname)[1].lower()
                        if img_ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                            continue
                            
                        img_bytes = z.read(name)
                        if len(img_bytes) < 15360:  # Ignore images smaller than 15 KB (icons/graphics)
                            continue
                        
                        safe_name = f"{file_path.stem}_{original_fname}"
                        safe_name = re.sub(r'[\\/*?:"<>|\'`]', "", safe_name).replace(" ", "_")
                        
                        img_local_path = temp_images_dir / safe_name
                        img_local_path.write_bytes(img_bytes)
                        extracted.append((safe_name, img_local_path))
                    except Exception as ex:
                        print(f"      [Warning] Failed to read media {name} from {file_path.name}: {ex}")
        except Exception as e:
            print(f"      [Warning] Failed to extract images from Zip-based attachment {file_path.name}: {e}")
            
    elif ext == ".pdf":
        from pypdf import PdfReader
        try:
            reader = PdfReader(file_path)
            img_idx = 1
            for page_num, page in enumerate(reader.pages, start=1):
                try:
                    for img_file in page.images:
                        img_bytes = img_file.data
                        if len(img_bytes) < 15360:  # Ignore images smaller than 15 KB (icons/graphics)
                            continue
                        
                        safe_name = f"{file_path.stem}_page{page_num}_img{img_idx}.png"
                        safe_name = re.sub(r'[\\/*?:"<>|\'`]', "", safe_name).replace(" ", "_")
                        
                        img_local_path = temp_images_dir / safe_name
                        img_local_path.write_bytes(img_bytes)
                        extracted.append((safe_name, img_local_path))
                        img_idx += 1
                except Exception as ex:
                    print(f"      [Warning] Failed to read images from page {page_num} in {file_path.name}: {ex}")
        except Exception as e:
            print(f"      [Warning] Failed to extract images from PDF attachment {file_path.name}: {e}")
            
    return extracted


def _parse_csv_in_chunks_locally(file_path: Path, chunk_size: int = 500):
    """Memory-safe chunk-by-chunk local CSV parser that splits massive files into multiple virtual files."""
    import pandas as pd
    chunk_num = 1
    try:
        # pd.read_csv in chunksize mode returns a generator that only loads one chunk at a time
        for chunk_df in pd.read_csv(file_path, chunksize=chunk_size):
            if chunk_df.empty:
                continue
            chunk_md = chunk_df.to_markdown(index=False)
            part_suffix = f" - Part {chunk_num}"
            enriched_content = (
                f"# CSV Data: {file_path.name} (Part {chunk_num})\n\n"
                f"*This document is Part {chunk_num} of the spreadsheet {file_path.name}.*\n\n"
                f"{chunk_md}"
            )
            yield part_suffix, enriched_content
            chunk_num += 1
            
            # Absolute hard-limit of 100 chunks (50k rows) to prevent disk space/API abuse
            if chunk_num > 100:
                print(f"     [Warning] Hard limit of 100 chunks (50,000 rows) reached for CSV {file_path.name}.")
                break
    except Exception as e:
        print(f"     [Warning] Failed to chunk-parse CSV locally: {e}. Falling back to standard read.")
        try:
            df = pd.read_csv(file_path, nrows=500)
            yield "", df.to_markdown(index=False)
        except Exception as ex:
            yield "", f"Failed to parse CSV locally: {ex}"


def _parse_excel_in_chunks_locally(file_path: Path, rows_per_chunk: int = 500):
    """Memory-safe Excel parser using openpyxl in read_only mode to stream sheets chunk-by-chunk.
    Avoids OOM by yielding multiple virtual sub-parts."""
    try:
        import pandas as pd
        from openpyxl import load_workbook
        
        # openpyxl read_only=True streams XML on demand and uses negligible memory (usually < 20MB)
        wb = load_workbook(filename=str(file_path), read_only=True, data_only=True)
        
        for sheet_name in wb.sheetnames:
            sheet = wb[sheet_name]
            rows_iter = sheet.iter_rows(values_only=True)
            try:
                header_row = next(rows_iter)
            except StopIteration:
                continue # Sheet is completely empty
                
            if not header_row or all(v is None for v in header_row):
                header_row = [] # fallback
                
            headers = [str(col) if col is not None else f"Col{i+1}" for i, col in enumerate(header_row)]
            
            chunk_rows = []
            chunk_num = 1
            
            for row in rows_iter:
                # Skip entirely empty rows to save memory and space
                if all(val is None for val in row):
                    continue
                row_vals = list(row)
                if len(row_vals) < len(headers):
                    row_vals += [None] * (len(headers) - len(row_vals))
                else:
                    row_vals = row_vals[:len(headers)]
                chunk_rows.append(row_vals)
                
                if len(chunk_rows) >= rows_per_chunk:
                    df = pd.DataFrame(chunk_rows, columns=headers)
                    chunk_md = df.to_markdown(index=False)
                    part_suffix = f" - Sheet {sheet_name} - Part {chunk_num}"
                    
                    enriched_content = (
                        f"# Excel Data: {file_path.name} (Sheet: {sheet_name}, Part {chunk_num})\n\n"
                        f"*This document is Sheet '{sheet_name}' (Part {chunk_num}) of the spreadsheet {file_path.name}.*\n\n"
                        f"{chunk_md}"
                    )
                    yield part_suffix, enriched_content
                    
                    # Dereference immediately
                    df = None
                    chunk_md = None
                    enriched_content = None
                    chunk_rows = []
                    chunk_num += 1
                    
                    if chunk_num > 40: # Limit 40 chunks per sheet to prevent runaway files
                        print(f"     [Warning] Limit of 40 chunks (20,000 rows) reached for sheet {sheet_name}.")
                        break
            
            # Package any remaining rows
            if chunk_rows:
                df = pd.DataFrame(chunk_rows, columns=headers)
                chunk_md = df.to_markdown(index=False)
                part_suffix = f" - Sheet {sheet_name}" if chunk_num == 1 else f" - Sheet {sheet_name} - Part {chunk_num}"
                enriched_content = (
                    f"# Excel Data: {file_path.name} (Sheet: {sheet_name}" + ("" if chunk_num == 1 else f", Part {chunk_num}") + ")\n\n"
                    f"*This document is Sheet '{sheet_name}' of the spreadsheet {file_path.name}.*\n\n"
                    f"{chunk_md}"
                )
                yield part_suffix, enriched_content
                
                # Dereference immediately
                df = None
                chunk_md = None
                enriched_content = None
                chunk_rows = []
                
        wb.close()
    except Exception as e:
        print(f"     [Warning] Failed streaming excel chunk-parse: {e}. Falling back to standard pandas read.")
        try:
            import pandas as pd
            xl = pd.ExcelFile(file_path)
            for sheet in xl.sheet_names:
                df = pd.read_excel(xl, sheet_name=sheet, nrows=500)
                yield f" - Sheet {sheet}", f"### Sheet: {sheet}\n\n" + df.to_markdown(index=False)
        except Exception as ex:
            yield "", f"Failed to parse Excel locally: {ex}"


def _is_pdf_searchable(file_path: Path) -> bool:
    """Checks if a PDF contains selectable text (is searchable). Returns False if it is a scanned image PDF."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        for i, page in enumerate(reader.pages):
            if i >= 3:  # Only check up to first 3 pages
                break
            text = page.extract_text()
            if text and len(text.strip()) > 20:
                return True
    except Exception as e:
        print(f"      [Warning] _is_pdf_searchable check failed for {file_path.name}: {e}")
    return False


def parse_attachment_to_markdown(
    file_path: Path,
    gemini_client,
    temp_images_dir: Path,
    bucket_name: str
) -> tuple[list[tuple[str, str]], list]:
    """Parses a local attachment of type .pdf, .docx, .pptx, .xlsx, .csv to markdown content.
    Also extracts and captions any embedded images.
    Returns a tuple of (list_of_content_parts, list_of_extracted_image_names)."""
    ext = file_path.suffix.lower()
    extracted_images = []
    
    # 0. Check if file is empty (0 bytes)
    if file_path.stat().st_size == 0:
        print(f"     [Warning] Attachment file {file_path.name} is empty (0 bytes). This can happen if the file is currently open in an online editor, or if it is an empty draft/placeholder.")
        empty_msg = f"# {file_path.name}\n\n*This document is empty (0 bytes). It may be actively open in an online editing session, or it is an empty placeholder.*"
        return [( "", empty_msg )], []

    # 1. Memory-safe chunk-splitting for spreadsheets
    if ext == ".csv":
        print(f"     -> Parsing CSV {file_path.name} using streaming chunk-splitter...")
        parts = _parse_csv_in_chunks_locally(file_path)
        return parts, []
    elif ext == ".xlsx":
        print(f"     -> Parsing Excel {file_path.name} using memory-safe streaming chunk-splitter...")
        parts = _parse_excel_in_chunks_locally(file_path)
        return parts, []

    # 2. Extract embedded images if applicable (PDF, DOCX, PPTX)
    if ext in (".pdf", ".docx", ".pptx"):
        print(f"     -> Extracting embedded images from {file_path.name}...")
        extracted_images = extract_images_from_attachment(file_path, temp_images_dir)
        if extracted_images:
            print(f"     -> Extracted {len(extracted_images)} image(s) from {file_path.name}.")
            
    # 3. Parse text & tables
    parsed_with_gemini = False
    content = ""
    
    # We use Gemini as the primary high-fidelity parser only for scanned / non-searchable PDF files
    if gemini_client and ext == ".pdf":
        is_searchable = _is_pdf_searchable(file_path)
        if is_searchable:
            print(f"     -> PDF {file_path.name} is searchable. Bypassing Gemini to parse locally (saves cost & latency)...")
            content = _parse_pdf_locally(file_path)
            parsed_with_gemini = False  # Bypassed Gemini
        else:
            print(f"     -> PDF {file_path.name} is scanned or non-searchable. Routing to Gemini for OCR parsing...")
            mime_type = "application/pdf"
            print(f"     -> Calling Gemini to parse {file_path.name}...")
            model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
            prompt = (
                "Please convert this document into a clean, structured Markdown representation. "
                "Retain all text, convert all tables into standard markdown tables. "
                "Do not include any HTML tags; use pure Markdown. Do not include markdown code block backticks surrounding the whole response."
            )
            try:
                file_bytes = file_path.read_bytes()
                with gemini_semaphore:
                    response = gemini_client.models.generate_content(
                        model=model_name,
                        contents=[
                            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                            prompt
                        ]
                    )
                content = response.text or ""
                if content.startswith("```markdown"):
                    content = content[11:]
                if content.startswith("```"):
                    content = content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()
                parsed_with_gemini = True
                print(f"     -> Gemini parsed {file_path.name} successfully.")
            except Exception as e:
                print(f"     [Warning] Gemini parser failed for {file_path.name}: {e}. Falling back to local parse.")
            
    # Local fallbacks
    if not parsed_with_gemini:
        print(f"     -> Parsing {file_path.name} with local parser fallback...")
        if ext == ".pdf":
            content = _parse_pdf_locally(file_path)
        elif ext == ".docx":
            content = _parse_docx_locally(file_path)
        elif ext == ".pptx":
            content = _parse_pptx_locally(file_path)
        elif ext == ".vsdx":
            content = _parse_vsdx_locally(file_path)
        elif ext == ".drawio":
            content = _parse_drawio_locally(file_path)
        # Note: CSV and Excel are already returned above.
                
    # 4. Append captioned embedded images to markdown
    if extracted_images:
        content += "\n\n## Embedded Images\n\n"
        
        # Helper function to caption a single extracted image
        def _caption_extracted_image(item):
            safe_name, img_local_path = item
            caption = "Image extracted from attachment."
            
            if not img_local_path.exists():
                return safe_name, caption
                
            file_size = img_local_path.stat().st_size
            ext_img = os.path.splitext(safe_name)[1].lower()
            supported_formats = (".png", ".jpg", ".jpeg", ".webp")
            
            # 1. Skip tiny images (< 15KB) like icons/graphics
            if file_size < 15360:
                return safe_name, "Small icon/graphic from attachment."
                
            # 2. Skip unsupported formats which fail on Gemini API
            if ext_img not in supported_formats:
                return safe_name, f"Embedded {ext_img.strip('.').upper()} diagram."
                
            if gemini_client:
                print(f"     -> [Parallel] Captioning embedded image {safe_name} ({file_size/1024:.1f} KB)...")
                mime = "image/png" if ext_img == ".png" else "image/jpeg"
                try:
                    img_bytes = img_local_path.read_bytes()
                    caption = describe_image_with_gemini(gemini_client, img_bytes, mime)
                except Exception as read_err:
                    print(f"     [Warning] Failed captioning {safe_name}: {read_err}")
            return safe_name, caption

        # Process extracted images in parallel (up to 4 workers)
        print(f"     -> Captioning {len(extracted_images)} extracted image(s) from {file_path.name} in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(extracted_images))) as img_executor:
            results = list(img_executor.map(_caption_extracted_image, extracted_images))
            
        for safe_name, caption in results:
            content += f"![{safe_name}](images/{safe_name})\n[IMAGE CAPTION: {caption}]\n\n"
            
    return [( "", content )], [item[0] for item in extracted_images]


def _get_oauth_access_token(client_id: str, client_secret: str) -> str:
    """Exchanges client credentials for an OAuth 2.0 machine-to-machine (2LO) access token."""
    resp = httpx.post(
        "https://auth.atlassian.com/oauth/token",
        json={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "audience": "api.atlassian.com",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _get_cloud_id(oauth_token: str) -> str | None:
    """Resolves the Atlassian Cloud ID for the site — required by api.atlassian.com download URLs.

    Priority:
      1. ATLASSIAN_CLOUD_ID env var (fastest — set this to skip the API call)
      2. api.atlassian.com/oauth/token/accessible-resources (requires app to be
         authorized against the site in Atlassian Admin > OAuth Apps > Authorize)
    """
    # Allow hardcoding the cloud ID to bypass the accessible-resources API
    # (needed when 2LO app has scopes but accessible-resources still returns [])
    env_cloud_id = os.getenv("ATLASSIAN_CLOUD_ID", "").strip()
    if env_cloud_id:
        print(f"  -> Using ATLASSIAN_CLOUD_ID from env: {env_cloud_id}")
        return env_cloud_id

    resp = httpx.get(
        "https://api.atlassian.com/oauth/token/accessible-resources",
        headers={"Authorization": f"Bearer {oauth_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    resources = resp.json()
    if resources:
        cloud_id = resources[0]["id"]
        print(f"  -> Resolved Atlassian Cloud ID: {cloud_id}")
        return cloud_id
    # Empty list means the OAuth app is not authorized against any site.
    # Fix: go to https://admin.atlassian.com → OAuth credentials → Authorize app
    # OR set ATLASSIAN_CLOUD_ID=<your-cloud-id> in .env
    print("  [Warning] accessible-resources returned an empty list.")
    print("            The 2LO OAuth app is not authorized against your Atlassian site.")
    print("            To fix: set ATLASSIAN_CLOUD_ID=<cloud-id> in .env")
    print("            Find your cloud ID at: https://admin.atlassian.com (site settings)")
    return None


def download_state_from_gcs(bucket_name: str, local_catalog_path: Path, local_map_path: Path, local_perm_path: Path = None):
    """Downloads catalog and mapping from GCS to local paths if they exist."""
    print("  -> Attempting to download ingestion state from GCS...")
    try:
        download_file_from_gcs(f"gs://{bucket_name}/ingestion_catalog.json", str(local_catalog_path))
        download_file_from_gcs(f"gs://{bucket_name}/gcs_confluence_map.json", str(local_map_path))
        if local_perm_path:
            download_file_from_gcs(f"gs://{bucket_name}/gcs_confluence_permissions_map.json", str(local_perm_path))
        
        if local_catalog_path.exists():
            print(f"     -> Successfully downloaded ingestion_catalog.json from gs://{bucket_name}")
        if local_map_path.exists():
            print(f"     -> Successfully downloaded gcs_confluence_map.json from gs://{bucket_name}")
        if local_perm_path and local_perm_path.exists():
            print(f"     -> Successfully downloaded gcs_confluence_permissions_map.json from gs://{bucket_name}")
    except Exception as e:
        print(f"     [Info] No existing state found on GCS or error downloading: {e}")

def upload_state_to_gcs(bucket_name: str, local_catalog_path: Path, local_map_path: Path, local_perm_path: Path = None):
    """Uploads the updated catalog and mapping back to GCS."""
    print("  -> Uploading updated ingestion state to GCS...")
    try:
        if local_catalog_path.exists():
            if upload_file_to_gcs(local_catalog_path, f"gs://{bucket_name}/ingestion_catalog.json"):
                print(f"     -> Uploaded ingestion_catalog.json to gs://{bucket_name}")
        if local_map_path.exists():
            if upload_file_to_gcs(local_map_path, f"gs://{bucket_name}/gcs_confluence_map.json"):
                print(f"     -> Uploaded gcs_confluence_map.json to gs://{bucket_name}")
        if local_perm_path and local_perm_path.exists():
            if upload_file_to_gcs(local_perm_path, f"gs://{bucket_name}/gcs_confluence_permissions_map.json"):
                print(f"     -> Uploaded gcs_confluence_permissions_map.json to gs://{bucket_name}")
    except Exception as e:
        print(f"     [Warning] Failed to upload state to GCS: {e}")

def process_single_page(
    page: dict,
    auth: tuple | None,
    conf_url: str,
    gemini_client,
    temp_images_dir: Path,
    catalog: dict,
    gcs_confluence_map: dict,
    force_reingest: bool,
    headers: dict | None = None,
    base_api: str | None = None,
    gcs_permissions_map: dict = None,
) -> tuple[list[dict], tuple[str, dict] | None]:
    """Processes a single Confluence page (extracts text, tables, images, and attachments).
    Returns a tuple: (list of generated document dicts, catalog update tuple or None if skipped)."""
    page_id = page["id"]
    title = page["title"]
    space_key = page.get("space_key", "UNKNOWN")
    version_info = page.get("version", {})
    version_num = version_info.get("number", 1)
    
    page_url = f"{conf_url.rstrip('/')}/wiki/spaces/{space_key}/pages/{page_id}"
    safe_filename = clean_filename(title)
    
    catalog_key = f"confluence_{page_id}"
    
    if not base_api:
        base_api = f"{conf_url.rstrip('/')}/wiki/rest/api"
    
    # Fetch page restrictions
    permissions = fetch_page_restrictions(base_api, page_id, auth, headers)
    permissions = permissions.copy()
    permissions["space_name"] = space_key
    
    bucket_name = os.getenv("RAG_GCS_BUCKET_NAME", "confluence-sharepoint-rag-bucket")
    main_uploaded_previously = False
    main_fn = None
    docs = []
    
    # Check if page is already cached and unmodified
    if not force_reingest and catalog_key in catalog:
        cached_entry = catalog[catalog_key]
        if cached_entry.get("version") == version_num:
            if cached_entry.get("attachments_fully_processed", False):
                # Skip download, formatting, captioning and parsing completely
                return [], None
            elif cached_entry.get("main_uploaded", False):
                main_uploaded_previously = True
                main_fn = cached_entry.get("filename")
                print(f"  -> Main content already cached for [{space_key}] {title}. Resuming attachments...")

    print(f"  [Ingest] Processing modified/new page: [{space_key}] {title} (version {version_num})")
    
    page_temp_images_dir = temp_images_dir / f"page_{page_id}"
    page_temp_images_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        def _replace_image_tag_thread(match: re.Match) -> str:
            nonlocal bucket_name
            fname_match = re.search(r'ri:filename="([^"]+)"', match.group(0))
            fname = fname_match.group(1) if fname_match else None
            if not fname:
                return "[IMAGE]"
            try:
                att_resp = httpx.get(
                    f"{base_api}/content/{page_id}/child/attachment",
                    auth=auth,
                    headers=headers,
                    params={"filename": fname, "expand": "version"},
                    timeout=30,
                )
                if att_resp.status_code != 200:
                    return f"[IMAGE: {fname}]"
                att_json = att_resp.json()
                att_results = att_json.get("results", [])
                if not att_results:
                    return f"[IMAGE: {fname}]"
                att = att_results[0]

                resp_base = att_json.get("_links", {}).get("base", f"{conf_url.rstrip('/')}/wiki")
                download_rel = att.get("_links", {}).get("download", "")
                
                download_urls = []
                download_urls.append(f"{resp_base.rstrip('/')}/rest/api/content/{page_id}/child/attachment/{att['id']}/download")
                if download_rel:
                    download_urls.append(f"{resp_base.rstrip('/')}{download_rel}")
                    download_urls.append(f"{resp_base.rstrip('/')}{download_rel}".split("?")[0])
                
                img_resp = None
                last_error = None
                for d_url in download_urls:
                    try:
                        resp = httpx.get(d_url, auth=auth, headers=headers, follow_redirects=True, timeout=60)
                        if resp.status_code == 200:
                            img_resp = resp
                            break
                        else:
                            last_error = f"HTTP {resp.status_code}"
                    except Exception as ex:
                        last_error = str(ex)
                        
                if not img_resp:
                    print(f"     [Warning] Image download failed for {fname} on page '{title}': {last_error}")
                    return f"[IMAGE: {fname}]"
                img_bytes = img_resp.content
                if len(img_bytes) < 15360 or img_bytes[:5] in (b"<html", b"<!DOC"):
                    return f"[IMAGE: {fname}]"
                    
                clean_fname = re.sub(r'[\\/*?:"<>|\'`]', "", fname).replace(" ", "_")
                safe_name = f"confluence_{page_id}_{clean_fname}"
                img_path = page_temp_images_dir / safe_name
                img_path.write_bytes(img_bytes)
                
                if not bucket_name:
                    bucket_name = "confluence-sharepoint-rag-bucket"
                gcs_img_url = f"https://storage.googleapis.com/{bucket_name}/images/{safe_name}"
                
                try:
                    if upload_file_to_gcs(img_path, f"gs://{bucket_name}/images/{safe_name}"):
                        print(f"     -> [GCS] Uploaded page image: {safe_name}")
                except Exception as ex:
                    print(f"     [Warning] Failed uploading image {safe_name}: {ex}")
                finally:
                    try:
                        if img_path.exists():
                            img_path.unlink()
                    except Exception as del_err:
                        print(f"     [Warning] Failed deleting local page image {safe_name}: {del_err}")

                ext_img = os.path.splitext(fname)[1].lower() if fname else ""
                supported_formats = (".png", ".jpg", ".jpeg", ".webp")
                if gemini_client and ext_img in supported_formats:
                    mime = "image/png" if ext_img == ".png" else "image/jpeg"
                    caption = describe_image_with_gemini(gemini_client, img_bytes, mime)
                    print(f"     -> Captioned page image: {fname}")
                    return f"![{fname}]({gcs_img_url})\n[IMAGE CAPTION: {caption}]"
                else:
                    if gemini_client and ext_img not in supported_formats:
                        print(f"     -> Skipping Gemini captioning for unsupported inline image format '{ext_img}': {fname}")
                    return f"![{fname}]({gcs_img_url})"
            except Exception as e:
                print(f"     [Warning] Could not process image {fname} on page '{title}': {e}")
                return f"[IMAGE: {fname}]"

        storage_content = page.get("body", {}).get("storage", {}).get("value", "")

        if not main_uploaded_previously:
            # Find and replace <ac:image> blocks with captions in parallel
            image_matches = list(re.finditer(r'<ac:image[^>]*>.*?</ac:image>', storage_content, flags=re.DOTALL))
            if image_matches:
                print(f"  -> Found {len(image_matches)} inline image(s) on page '{title}'. Processing in parallel...")
                
                # Helper to process a single match and return (original_text, replacement)
                def _process_single_match(m: re.Match) -> tuple[str, str]:
                    original_text = m.group(0)
                    try:
                        # We reuse the existing logic in _replace_image_tag_thread
                        replacement = _replace_image_tag_thread(m)
                        return original_text, replacement
                    except Exception as ex:
                        print(f"     [Warning] Match processing failed: {ex}")
                        return original_text, "[IMAGE]"
                        
                # Process inline images in parallel (up to 5 workers)
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(image_matches))) as img_executor:
                    results = list(img_executor.map(_process_single_match, image_matches))
                    
                # Replace the matched text segments one-by-one
                for original_text, replacement in results:
                    storage_content = storage_content.replace(original_text, replacement, 1)

            # Convert HTML tables to Markdown
            storage_content = _convert_html_tables_to_markdown(storage_content)
            plain_text = _strip_html(storage_content)
            
            main_doc = {
                "id": catalog_key,
                "title": title,
                "content": plain_text,
                "source": "Confluence",
                "url": page_url,
                "restricted": permissions.get("restricted", False),
                "allowed_users": permissions.get("allowed_users", []),
                "allowed_groups": permissions.get("allowed_groups", []),
                "space_name": permissions.get("space_name", ""),
            }
            docs.append(main_doc)

            # 1. Upload main page document first!
            main_fn = save_and_upload_markdown_doc(main_doc, page_temp_images_dir, bucket_name)
            with state_lock:
                gcs_confluence_map[main_fn] = page_url
                if gcs_permissions_map is not None:
                    gcs_permissions_map[main_fn] = permissions
                catalog[catalog_key] = {
                    "version": version_num,
                    "title": title,
                    "filename": main_fn,
                    "space_key": space_key,
                    "main_uploaded": True,
                    "attachments_fully_processed": False,
                }
                save_state_incrementally(catalog, gcs_confluence_map, bucket_name, gcs_permissions_map)
        else:
            print(f"  -> Main content already uploaded for [{space_key}] {title}. Jumping directly to attachments.")

        # Fetch and process attachments (PDF, DOCX, PPTX, XLSX, CSV)
        try:
            att_resp = httpx.get(
                f"{base_api}/content/{page_id}/child/attachment",
                auth=auth,
                headers=headers,
                timeout=30,
            )
            if att_resp.status_code == 200:
                attachments = att_resp.json().get("results", [])
                for att in attachments:
                    att_title = att.get("title", "")
                    att_ext = os.path.splitext(att_title)[1].lower()
                    if att_ext in (".pdf", ".docx", ".pptx", ".xlsx", ".csv"):
                        att_id = att["id"]
                        att_version = att.get("version", {}).get("number", 1)
                        att_catalog_key = f"confluence_attachment_{att_id}"
                        
                        # Check if attachment is already cached and unmodified
                        is_cached = False
                        with state_lock:
                            if not force_reingest and att_catalog_key in catalog:
                                cached_att = catalog[att_catalog_key]
                                if cached_att.get("version") == att_version:
                                    is_cached = True
                                    cached_filenames = cached_att.get("filenames", [])
                                    print(f"  -> Skipping cached attachment: {att_title} (version {att_version}) on page [{space_key}] {title}")
                                    # Restore mappings to gcs_confluence_map
                                    for fn in cached_filenames:
                                        gcs_confluence_map[fn] = page_url
                                        if gcs_permissions_map is not None:
                                            gcs_permissions_map[fn] = permissions

                        if not is_cached:
                            print(f"  -> Found attachment: {att_title} on page [{space_key}] {title}")
                            atts_temp_dir = page_temp_images_dir / "raw_attachments"
                            atts_temp_dir.mkdir(exist_ok=True)
                            
                            local_att_path = download_attachment(conf_url, auth, page_id, att, atts_temp_dir, headers=headers)
                            if not local_att_path or not local_att_path.exists():
                                raise RuntimeError(f"Attachment download failed for: {att_title}")
                                
                            try:
                                content_parts, extracted_image_names = parse_attachment_to_markdown(
                                    local_att_path,
                                    gemini_client,
                                    page_temp_images_dir,
                                    bucket_name
                                )
                                    
                                # Upload extracted images to GCS and delete them immediately
                                for safe_name in extracted_image_names:
                                    img_path = page_temp_images_dir / safe_name
                                    if img_path.exists():
                                        try:
                                            if upload_file_to_gcs(img_path, f"gs://{bucket_name}/images/{safe_name}"):
                                                print(f"     -> [GCS] Uploaded extracted image: {safe_name}")
                                        except Exception as ex:
                                            print(f"     [Warning] Failed uploading extracted image {safe_name}: {ex}")
                                        finally:
                                            try:
                                                if img_path.exists():
                                                    img_path.unlink()
                                            except Exception as del_err:
                                                print(f"     [Warning] Failed deleting local extracted image {safe_name}: {del_err}")
                                
                                # Create separate documents for each chunk/part
                                att_id_base = f"confluence_{page_id}_attachment_{att['id']}"
                                att_doc_title_base = f"[Attachment] {title} - {att_title}"
                                
                                uploaded_filenames = []
                                for part_suffix, part_content in content_parts:
                                    part_id = att_id_base
                                    if part_suffix:
                                        safe_suffix = part_suffix.lower().replace(" ", "_").replace("-", "_").strip("_")
                                        part_id = f"{att_id_base}_{safe_suffix}"
                                    part_doc_title = f"{att_doc_title_base}{part_suffix}"
                                    
                                    doc = {
                                        "id": part_id,
                                        "title": part_doc_title,
                                        "content": part_content,
                                        "source": "Confluence",
                                        "url": page_url,
                                        "restricted": permissions.get("restricted", False),
                                        "allowed_users": permissions.get("allowed_users", []),
                                        "allowed_groups": permissions.get("allowed_groups", []),
                                        "space_name": permissions.get("space_name", ""),
                                    }
                                    
                                    # Upload immediately!
                                    fn = save_and_upload_markdown_doc(doc, page_temp_images_dir, bucket_name)
                                    uploaded_filenames.append(fn)
                                    with state_lock:
                                        gcs_confluence_map[fn] = page_url
                                        if gcs_permissions_map is not None:
                                            gcs_permissions_map[fn] = permissions
                                    
                                    print(f"     -> Successfully prepared & uploaded attachment document: {part_doc_title}")
                                
                                # Update catalog for this attachment immediately
                                with state_lock:
                                    catalog[att_catalog_key] = {
                                        "version": att_version,
                                        "title": att_title,
                                        "filenames": uploaded_filenames,
                                        "page_id": page_id,
                                        "space_key": space_key,
                                    }
                                    # Save state files and upload if needed
                                    save_state_incrementally(catalog, gcs_confluence_map, bucket_name, gcs_permissions_map)
                                        
                            finally:
                                try:
                                    if local_att_path and local_att_path.exists():
                                        local_att_path.unlink()
                                        print(f"     -> Deleted temporary attachment file: {local_att_path.name}")
                                except Exception as del_err:
                                    print(f"     [Warning] Failed deleting temporary attachment file: {del_err}")
        except Exception as e:
            print(f"     [Warning] Failed processing attachments for page '{title}': {e}")
            with state_lock:
                confluence_failures.append({
                    "file_name": f"Attachments of page: {title}",
                    "error_code": type(e).__name__,
                    "failure_reason": str(e),
                    "space_name": space_key
                })

    finally:
        try:
            if page_temp_images_dir.exists():
                shutil.rmtree(page_temp_images_dir)
        except Exception as del_err:
            print(f"     [Warning] Failed to clean up page-specific temp directory {page_temp_images_dir.name}: {del_err}")

    new_catalog_entry = {
        "version": version_num,
        "title": title,
        "filename": main_fn,
        "space_key": space_key,
        "main_uploaded": True,
        "attachments_fully_processed": True,
    }
    # Clear content of main_doc before returning to save memory
    if docs:
        docs[0]["content"] = ""
    return docs, (catalog_key, new_catalog_entry)

def fetch_confluence_pages(
    conf_url: str,
    conf_user: str | None,
    conf_token: str | None,
    gemini_client,
    temp_images_dir: Path,
    catalog: dict,
    gcs_confluence_map: dict,
    force_reingest: bool = False,
) -> list:
    """Fetches all pages from Confluence via REST API in parallel batches, downloads attached
    images/documents, captions them with Gemini, and returns only the newly added/modified docs."""
    auth = (conf_user, conf_token) if (conf_user and conf_token) else None
    headers = {}

    # Resolve OAuth Bearer token for attachment downloads & API access.
    oauth_token = os.getenv("CONFLUENCE_OAUTH_TOKEN")
    if not oauth_token:
        client_id = os.getenv("CONFLUENCE_OAUTH_CLIENT_ID")
        client_secret = os.getenv("CONFLUENCE_OAUTH_CLIENT_SECRET")
        if client_id and client_secret:
            try:
                oauth_token = _get_oauth_access_token(client_id, client_secret)
                print("  -> Obtained OAuth 2.0 access token via client credentials.")
            except Exception as e:
                print(f"  [Warning] OAuth token exchange failed: {e}. Images will use placeholders.")
    if oauth_token:
        headers["Authorization"] = f"Bearer {oauth_token}"
    else:
        print("  [Info] No CONFLUENCE_OAUTH_TOKEN or CONFLUENCE_OAUTH_CLIENT_ID/SECRET set.")
        print("         Image attachments will be saved as [IMAGE: filename] placeholders.")

    cloud_id = None
    if oauth_token:
        try:
            cloud_id = _get_cloud_id(oauth_token)
        except Exception as e:
            print(f"  [Warning] Could not resolve Cloud ID: {e}. Image downloads will be skipped.")
            cloud_id = None

    if cloud_id and not auth:
        base_api = f"https://api.atlassian.com/ex/confluence/{cloud_id}/rest/api"
        print(f"  -> Operating in OAuth 2.0 API gateway mode with Base API: {base_api}")
    else:
        base_api = f"{conf_url.rstrip('/')}/wiki/rest/api"
        print(f"  -> Operating in Basic Auth mode with Base API: {base_api}")

    # Optional: restrict to specific space keys via CONFLUENCE_SPACES=KEY1,KEY2
    space_filter = [s.strip() for s in os.getenv("CONFLUENCE_SPACES", "").split(",") if s.strip()]

    print("  -> Fetching Confluence spaces...")
    all_spaces = []
    start = 0
    try:
        while True:
            spaces_resp = httpx.get(
                f"{base_api}/space",
                auth=auth,
                headers=headers,
                params={"limit": 100, "start": start},
                timeout=30,
            )
            spaces_resp.raise_for_status()
            page_data = spaces_resp.json()
            batch = page_data.get("results", [])
            all_spaces.extend(batch)
            print(f"  -> Found {len(batch)} space(s) in batch (start={start}): {[s['key'] for s in batch]}")
            if not batch or len(batch) < 100:
                break
            start += 100
    except Exception as e:
        print(f"  [Error] Could not fetch Confluence spaces: {e}")
        return []

    #space_keys = (
    #    [s["key"] for s in all_spaces if s["key"] in space_filter]
    #    if space_filter
    #    else [s["key"] for s in all_spaces]
    #)
    space_keys = set(space_filter)
    print(f"  -> Processing {len(space_keys)} space(s): {space_keys}")

    # Gather ALL pages first (super-fast metadata scan)
    all_pages_to_process = []
    has_fetch_errors = False
    for space_key in space_keys:
        start = 0
        limit = 50
        while True:
            try:
                # Robust exponential backoff retries & longer timeout for fetching space pages
                data = {}
                retries = 3
                backoff = 2
                import time
                for attempt in range(retries):
                    try:
                        pages_resp = httpx.get(
                            f"{base_api}/content",
                            auth=auth,
                            headers=headers,
                            params={
                                "spaceKey": space_key,
                                "type": "page",
                                "start": start,
                                "limit": limit,
                                "expand": "body.storage,version",
                            },
                            timeout=90,
                        )
                        pages_resp.raise_for_status()
                        data = pages_resp.json()
                        break # Success! Break retry loop
                    except httpx.HTTPStatusError as http_err:
                        if http_err.response.status_code == 429:
                            retry_after = int(http_err.response.headers.get("Retry-After", backoff * (attempt + 1)))
                            print(f"     [Warning] Rate-limited (HTTP 429) fetching pages for space {space_key}. Retrying after {retry_after}s...")
                            time.sleep(retry_after)
                        else:
                            if attempt == retries - 1:
                                raise
                            time.sleep(backoff ** attempt)
                    except (httpx.ReadTimeout, httpx.ConnectTimeout) as timeout_err:
                        print(f"     [Warning] Timeout fetching pages for space {space_key} (Attempt {attempt+1}/{retries}): {timeout_err}")
                        if attempt == retries - 1:
                            raise
                        time.sleep(backoff ** attempt)
                    except Exception as other_err:
                        if attempt == retries - 1:
                            raise
                        time.sleep(backoff ** attempt)
            except Exception as e:
                print(f"  [Error] Could not fetch pages for space {space_key}: {e}")
                has_fetch_errors = True
                confluence_failures.append({
                    "file_name": f"Space_{space_key}",
                    "error_code": type(e).__name__,
                    "failure_reason": f"Could not fetch space pages directly: {str(e)}",
                    "space_name": space_key
                })
                break

            results = data.get("results", [])
            for page in results:
                page["space_key"] = space_key
                all_pages_to_process.append(page)

            if not results or len(results) < limit:
                break
            start += limit

    print(f"  -> Discovered {len(all_pages_to_process)} total page(s) in Confluence.")

    # 1. Sync Confluence page deletions back to GCS and update catalog (with Defensive Guardrails)
    fetched_page_ids = {f"confluence_{page['id']}" for page in all_pages_to_process}
    bucket_name = os.getenv("RAG_GCS_BUCKET_NAME")
    
    if bucket_name and bucket_name != "YOUR_BUCKET_NAME":
        # Calculate existing page count in catalog
        catalog_confluence_pages_count = sum(1 for k in catalog if k.startswith("confluence_") and "_attachment_" not in k)
        
        # Check Guardrail 1: API Connection/Fetch Errors
        if has_fetch_errors:
            print("\n" + "="*80)
            print("[CRITICAL SAFETY WARNING] One or more API errors occurred while fetching Confluence pages.")
            print("To prevent accidental data loss due to incomplete page discovery, DELETE SYNC HAS BEEN AUTO-PAUSED.")
            print("No files were deleted from GCS, and no catalog entries were purged.")
            print("="*80 + "\n")
            
        # Check Guardrail 2: Accidental Massive Deletion (Service Account Expiration)
        elif catalog_confluence_pages_count > 10 and len(all_pages_to_process) == 0:
            print("\n" + "="*80)
            print(f"[CRITICAL SAFETY WARNING] Confluence API returned 0 pages, but your catalog contains "
                  f"{catalog_confluence_pages_count} existing Confluence page(s).")
            print("This is highly indicative of service account expiration or permission loss!")
            print("To prevent accidental data loss and protect your GCS storage, DELETE SYNC HAS BEEN AUTO-PAUSED.")
            print("No files were deleted from GCS, and no catalog entries were purged.")
            print("Please check and verify your Confluence Service Account credentials.")
            print("="*80 + "\n")
            
        else:
            deleted_catalog_keys = []
            for catalog_key, entry in list(catalog.items()):
                if catalog_key.startswith("confluence_") and "_attachment_" not in catalog_key:
                    if catalog_key not in fetched_page_ids:
                        # Only purge if it belongs to the synced spaces
                        space_key_in_catalog = entry.get("space_key")
                        if not space_key_in_catalog or space_key_in_catalog in space_keys:
                            deleted_catalog_keys.append(catalog_key)
            
            if deleted_catalog_keys:
                print(f"\n  [Delete Sync] Detected {len(deleted_catalog_keys)} page(s) deleted from Confluence. Syncing purges...")
                for del_key in deleted_catalog_keys:
                    entry = catalog[del_key]
                    del_filename = entry.get("filename")
                    print(f"     -> Purging deleted page: {entry.get('title')} (GCS: {del_filename})")
                    if del_filename:
                        try:
                            delete_file_from_gcs(f"gs://{bucket_name}/{del_filename}")
                        except Exception as ex:
                            print(f"     [Warning] Failed deleting gs://{bucket_name}/{del_filename}: {ex}")
                    catalog.pop(del_key, None)
                    if del_filename:
                        gcs_confluence_map.pop(del_filename, None)

                    # Also find and purge all associated attachments!
                    del_page_id = del_key[11:]  # "confluence_12345" -> "12345"
                    for att_key, att_entry in list(catalog.items()):
                        if att_key.startswith("confluence_attachment_"):
                            if str(att_entry.get("page_id")) == str(del_page_id):
                                # Purge attachment files!
                                att_filenames = att_entry.get("filenames", [])
                                print(f"        -> Purging deleted attachment: {att_entry.get('title')} (GCS: {att_filenames})")
                                for att_fn in att_filenames:
                                    try:
                                        delete_file_from_gcs(f"gs://{bucket_name}/{att_fn}")
                                    except Exception as ex:
                                        print(f"     [Warning] Failed deleting gs://{bucket_name}/{att_fn}: {ex}")
                                    gcs_confluence_map.pop(att_fn, None)
                                catalog.pop(att_key, None)

    # 2. Run multi-threaded ingestion for modified or new pages
    concurrency = int(os.getenv("CONFLUENCE_INGEST_CONCURRENCY", "3"))
    print(f"  -> Launching multi-threaded crawl & Gemini captioning with {concurrency} threads...")
    
    docs = []
    skipped_count = 0
    processed_count = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                process_single_page,
                page,
                auth,
                conf_url,
                gemini_client,
                temp_images_dir,
                catalog,
                gcs_confluence_map,
                force_reingest,
                headers=headers,
                base_api=base_api,
                gcs_permissions_map=gcs_permissions_map,
            ): page
            for page in all_pages_to_process
        }

        for future in concurrent.futures.as_completed(futures):
            page = futures[future]
            try:
                result_docs, catalog_update = future.result()
                if catalog_update:
                    cat_key, cat_entry = catalog_update
                    cat_entry["space_key"] = page.get("space_key")
                    with state_lock:
                        catalog[cat_key] = cat_entry
                        docs.extend(result_docs)
                        processed_count += 1
                        
                        # Update URL mapping in real-time
                        for doc in result_docs:
                            filename = clean_filename(doc["title"])
                            gcs_confluence_map[filename] = doc["url"]
                            
                        # Save incremental/intermittent state to GCS to safeguard against timeouts
                        if bucket_name and bucket_name != "YOUR_BUCKET_NAME":
                            save_state_incrementally(catalog, gcs_confluence_map, bucket_name, gcs_permissions_map)
                else:
                    skipped_count += 1
            except Exception as e:
                print(f"  [Error] Thread processing failed for page '{page.get('title')}': {e}")
                with state_lock:
                    confluence_failures.append({
                        "file_name": page.get("title", "Unknown"),
                        "error_code": type(e).__name__,
                        "failure_reason": str(e),
                        "space_name": page.get("space_key", "UNKNOWN")
                    })

    print(f"\n  [Ingestion Job Statistics]")
    print(f"     Confluence pages checked:      {len(all_pages_to_process)}")
    print(f"     Skipped (unmodified on cache): {skipped_count}")
    print(f"     Processed (modified or new):   {processed_count}")
    print(f"     Generated RAG documents:       {len(docs)}")

    return docs


def download_and_caption_public_sample(client: genai.Client, temp_images_dir: Path) -> str:
    """Demonstrates live visual grounding by downloading a public diagram and generating its caption."""
    # Use a stable gstatic sample image
    sample_diagram_url = "https://www.gstatic.com/webp/gallery/1.png"
    image_path = temp_images_dir / "psc_architecture.png"
    
    print(f"  -> Downloading sample diagram from: {sample_diagram_url}")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        req = urllib.request.Request(sample_diagram_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            image_bytes = response.read()
            
        with open(image_path, "wb") as f:
            f.write(image_bytes)
        print(f"  -> Saved sample raw image to: {image_path.name}")
        
        # Caption it using Gemini
        print("  -> Calling live Gemini API for visual captioning/grounding...")
        caption = describe_image_with_gemini(client, image_bytes, mime_type="image/png")
        print(f"  -> Generated Caption:\n\n{caption}\n")
        return caption
    except Exception as e:
        print(f"  [Warning] Failed to download or caption sample image: {e}")
        return "Grounded caption generation bypassed."

def main():
    temp_dir = workspace_dir / "gcs_upload_temp"
    temp_images_dir = temp_dir / "images"
    
    # 1. Check for real Confluence credentials & bucket configuration
    bucket_name = os.getenv("RAG_GCS_BUCKET_NAME")
    if not bucket_name:
         print("\nWarning: RAG_GCS_BUCKET_NAME is not set in your .env file.")
         bucket_name = "YOUR_BUCKET_NAME"

    # Define paths for our ingestion cache files
    catalog_filepath = Path(__file__).parent / "ingestion_catalog.json"
    map_filepath = Path(__file__).parent / "gcs_confluence_map.json"
    perm_filepath = Path(__file__).parent / "gcs_confluence_permissions_map.json"

    # 2. Download the latest state from GCS (enables stateless/distributed scaling)
    if bucket_name != "YOUR_BUCKET_NAME":
        download_state_from_gcs(bucket_name, catalog_filepath, map_filepath, perm_filepath)

    # Load ingestion catalog
    catalog = {}
    if catalog_filepath.exists():
        try:
            with open(catalog_filepath, "r", encoding="utf-8") as f:
                catalog = json.load(f)
            print(f"  -> Loaded existing ingestion catalog with {len(catalog)} page entry(s).")
        except Exception as e:
            print(f"  [Warning] Failed loading catalog: {e}. Starting fresh.")

    # Load URL mappings
    gcs_confluence_map = {}
    if map_filepath.exists():
        try:
            with open(map_filepath, "r", encoding="utf-8") as f:
                gcs_confluence_map = json.load(f)
            print(f"  -> Loaded existing GCS Confluence URL map with {len(gcs_confluence_map)} entry(s).")
        except Exception as e:
            print(f"  [Warning] Failed loading GCS map: {e}. Starting fresh.")

    # Load Permissions Map
    global gcs_permissions_map
    gcs_permissions_map = {}
    if perm_filepath.exists():
        try:
            with open(perm_filepath, "r", encoding="utf-8") as f:
                gcs_permissions_map = json.load(f)
            print(f"  -> Loaded existing GCS Permissions Map with {len(gcs_permissions_map)} entry(s).")
        except Exception as e:
            print(f"  [Warning] Failed loading GCS permissions map: {e}. Starting fresh.")

    # Recreate clean local temporary upload directory
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_images_dir.mkdir(parents=True, exist_ok=True)
    
    # Check for credentials
    try:
        client = get_gemini_client()
        has_gemini = True
    except Exception as e:
        print(f"Warning: Could not initialize Google GenAI client: {e}. Fallback to pre-defined descriptions.")
        client = None
        has_gemini = False

    conf_url = os.getenv("CONFLUENCE_URL")
    conf_user = os.getenv("CONFLUENCE_USERNAME")
    conf_token = os.getenv("CONFLUENCE_API_TOKEN")
    force_reingest = os.getenv("FORCE_REINGEST", "false").lower() == "true"

    oauth_client_id = os.getenv("CONFLUENCE_OAUTH_CLIENT_ID")
    oauth_client_secret = os.getenv("CONFLUENCE_OAUTH_CLIENT_SECRET")

    if conf_url and ((conf_user and conf_token) or (oauth_client_id and oauth_client_secret)):
        print("=" * 80)
        print("RUNNING PRODUCTION CONFLUENCE EXTRACTION & IMAGE CAPTIONING")
        print("=" * 80)
        docs = fetch_confluence_pages(
            conf_url,
            conf_user,
            conf_token,
            client if has_gemini else None,
            temp_images_dir,
            catalog,
            gcs_confluence_map,
            force_reingest,
        )
        if not docs and not catalog:
            print("  [Warning] No pages returned from Confluence and catalog is empty. Falling back to mock data.")
            docs = get_mock_confluence_data() + get_mock_sharepoint_data()
    else:
        print("=" * 80)
        print("RUNNING DEMO EXTRACTION WITH LIVE IMAGE CAPTIONING & GCS UPLOAD")
        print("=" * 80)

        # Run the live image download and Gemini captioning demonstration
        caption = "Mock caption used due to offline/missing credentials."
        if has_gemini and client:
            caption = download_and_caption_public_sample(client, temp_images_dir)

        docs = get_mock_confluence_data() + get_mock_sharepoint_data()

        # Inject the live Gemini-generated caption into the mock PSC diagram placeholder
        for doc in docs:
            if "![PSC Architecture Diagram](images/psc_architecture.png)" in doc.get("content", ""):
                doc["content"] = doc["content"].replace(
                    "![PSC Architecture Diagram](images/psc_architecture.png)\n"
                    "[IMAGE CONTENT (GROUNDED CAPTION): Architecture diagram displaying the PSC routing path. The client request hits the Itinerary Orchestrator at the entrypoint. The Orchestrator forwards payloads to the Flight Planning Agent and Sightseeing Agent. The Flight agent connects through the psc-flight-svc endpoint to a regional Cloud Run container. The Sightseeing agent routes requests through the psc-sight-svc endpoint to another backend service, utilizing native VPC Peering.]",
                    f"![PSC Architecture Diagram](images/psc_architecture.png)\n"
                    f"[IMAGE CONTENT (LIVE GEMINI CAPTION): {caption}]",
                )

    is_demo_mode = not (conf_url and ((conf_user and conf_token) or (oauth_client_id and oauth_client_secret)))
    if docs:
        if is_demo_mode:
            print(f"\nCreating local temporary documents under: {temp_dir}\n")

            for doc in docs:
                filename = clean_filename(doc["title"])
                filepath = temp_dir / filename
                gcs_confluence_map[filename] = doc["url"]

                content = doc["content"]

                # Replace any local image paths with public GCS URLs so the RAG engine
                # stores and returns fully qualified image links the agent can cite in UI.
                content = re.sub(
                    r'!\[([^\]]*)\]\(images/([^)]+)\)',
                    lambda m: f"![{m.group(1)}](https://storage.googleapis.com/{bucket_name}/images/{m.group(2)})",
                    content,
                )

                enriched_content = (
                    f"source_url: {doc['url']}\n"
                    f"title: {doc['title']}\n"
                    f"source_system: {doc['source']}\n\n"
                    f"{content}\n\n"
                    f"---\n"
                    f"doc_id: {doc['id']}\n"
                    f"source_url: {doc['url']}\n"
                )

                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(enriched_content)
                print(f"  -> Created file: {filename} ({len(enriched_content)} bytes)")
        else:
            for doc in docs:
                filename = clean_filename(doc["title"])
                gcs_confluence_map[filename] = doc["url"]

    # Save the updated mapping and catalog files locally
    with open(map_filepath, "w", encoding="utf-8") as f:
        json.dump(gcs_confluence_map, f, indent=2)
    print(f"Saved updated GCS to Confluence URL mapping locally to: {map_filepath}")

    with open(catalog_filepath, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2)
    print(f"Saved updated Ingestion Catalog locally to: {catalog_filepath}")

    with open(perm_filepath, "w", encoding="utf-8") as f:
        json.dump(gcs_permissions_map, f, indent=2)
    print(f"Saved updated GCS Permissions Map locally to: {perm_filepath}")

    # Auto-run GCS sync if bucket is configured
    if bucket_name != "YOUR_BUCKET_NAME":
        if is_demo_mode:
            markdown_files = list(temp_dir.glob("*.md"))
            if markdown_files:
                print(f"\nUploading {len(markdown_files)} new/modified .md files to gs://{bucket_name}/...")
                markdown_uploaded = 0
                for md_path in markdown_files:
                    try:
                        if upload_file_to_gcs(md_path, f"gs://{bucket_name}/{md_path.name}"):
                            markdown_uploaded += 1
                    except Exception as e:
                        print(f"  [Warning] Failed uploading markdown {md_path.name}: {e}")
                print(f"  Uploaded {markdown_uploaded} markdown documents successfully!")
            else:
                print("\nNo new or modified markdown documents to upload.")

            image_files = list(temp_images_dir.iterdir()) if temp_images_dir.exists() else []
            if image_files:
                print(f"Uploading {len(image_files)} image(s) to gs://{bucket_name}/images/...")
                images_uploaded = 0
                for img_path in image_files:
                    try:
                        if upload_file_to_gcs(img_path, f"gs://{bucket_name}/images/{img_path.name}"):
                            images_uploaded += 1
                    except Exception as ex:
                        print(f"     [Warning] Failed uploading image {img_path.name}: {ex}")
                print(f"  Uploaded {images_uploaded} image(s).")
            else:
                print("No new images to upload.")

        # Upload updated state back to GCS
        upload_state_to_gcs(bucket_name, catalog_filepath, map_filepath, perm_filepath)

        # Upload failure results report to GCS
        try:
            local_failures_path = Path(__file__).parent / "gcs_confluence_failure_results.json"
            local_failures_path.write_text(json.dumps(confluence_failures, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"\n  -> Uploading {len(confluence_failures)} failure results to gs://{bucket_name}/gcs_confluence_failure_results.json...")
            upload_file_to_gcs(local_failures_path, f"gs://{bucket_name}/gcs_confluence_failure_results.json")
        except Exception as fail_err:
            print(f"  [Warning] Failed writing or uploading failure results: {fail_err}")

        # Clean up local temp directory after upload
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        print("Cleaned up temporary local folder.")

if __name__ == "__main__":
    main()




