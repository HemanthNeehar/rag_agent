import os
import re
import sys
import shutil
import subprocess
import urllib.request
import httpx
import json
import concurrent.futures
import threading
import base64
import zlib
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load env from workspace root
workspace_dir = Path(__file__).parent.parent
load_dotenv(workspace_dir / ".env")

# Thread-safe semaphore to restrict concurrent Gemini API calls and prevent rate limiting (429 errors)
gemini_semaphore = threading.Semaphore(3)

# Global permissions map to match GCS files with dynamic enterprise permissions
gcs_permissions_map = {}
state_lock = threading.Lock()
sharepoint_failures = []

def clean_filename(title: str) -> str:
    """Removes special characters to make a safe filename."""
    name = re.sub(r'[\\/*?:"<>|\'`]', "", title)
    name = name.replace(" ", "_")
    if not name.endswith(".md"):
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
    
    if not texts:
        return "Empty Visio Diagram or text extraction yielded no results."
    return "\n\n".join(texts)

def _parse_drawio_locally(file_path: Path) -> str:
    """Extracts text and labels from Draw.io XML files (supports raw XML and compressed formats)."""
    import xml.etree.ElementTree as ET
    texts = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore").strip()
        if not content:
            return "Empty Draw.io file."
            
        # Parse XML
        root = ET.fromstring(content)
        
        # Draw.io files can have diagrams compressed inside <diagram> tags
        diagram_nodes = root.findall('.//diagram')
        if diagram_nodes:
            for idx, diag in enumerate(diagram_nodes):
                diag_text = diag.text
                if diag_text:
                    try:
                        # Decompress: base64 decode -> decompress with negative wbits -> URL decode
                        decoded_bytes = base64.b64decode(diag_text.strip())
                        decompressed_bytes = zlib.decompress(decoded_bytes, -zlib.MAX_WBITS)
                        decompressed_text = urllib.parse.unquote(decompressed_bytes.decode('utf-8'))
                        
                        # Parse decompressed XML
                        diag_root = ET.fromstring(decompressed_text)
                        for cell in diag_root.findall('.//mxCell'):
                            value = cell.get('value')
                            if value:
                                # Strip HTML tags from value
                                clean_val = re.sub(r'<[^>]+>', ' ', value).strip()
                                # Unescape HTML entities
                                clean_val = urllib.parse.unquote(clean_val)
                                if clean_val:
                                    texts.append(clean_val)
                    except Exception as decompress_err:
                        print(f"      [Info] Could not decompress Draw.io diagram {idx}: {decompress_err}. Trying plain parse.")
        
        # Also scan the XML directly for any standard text attributes
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
                        
                        original_fname = os.path.basename(name)
                        safe_name = f"{file_path.stem}_{original_fname}"
                        safe_name = re.sub(r'[\\/*?:"<>|\'`]', "", safe_name).replace(" ", "_")
                        
                        img_local_path = temp_images_dir / safe_name
                        img_local_path.write_bytes(img_bytes)
                        extracted.append((safe_name, img_local_path))
                    except Exception as ex:
                        print(f"      [Warning] Failed to read media {name} from {file_path.name}: {ex}")
        except Exception as e:
            print(f"      [Warning] Failed to extract images from Zip-based attachment {file_path.name}: {e}")
            try:
                snippet = file_path.read_bytes()[:500]
                print(f"      [Debug] First 500 bytes of {file_path.name}: {snippet}")
            except Exception as read_err:
                print(f"      [Debug] Failed to read file bytes: {read_err}")
            
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
    """Parses a local attachment to markdown content.
    Also extracts and captions any embedded images.
    Returns a tuple of (list_of_content_parts, list_of_extracted_image_names)."""
    ext = file_path.suffix.lower()
    
    # 0. Check if file is empty (0 bytes)
    if file_path.stat().st_size == 0:
        print(f"     [Warning] SharePoint file {file_path.name} is empty (0 bytes). This can happen if the file is currently open in PowerPoint/Word Online, or if it is an empty draft/placeholder.")
        empty_msg = f"# {file_path.name}\n\n*This document is empty (0 bytes on SharePoint). It may be actively open in an online editing session, or it is an empty placeholder.*"
        return [("", empty_msg)], []

    # Initialize extracted_images
    extracted_images = []

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
        elif ext in (".txt", ".py"):
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                if ext == ".py":
                    content = f"```python\n{content}\n```"
            except Exception as e:
                content = f"Failed to read raw file {file_path.name}: {e}"
        elif ext == ".aspx":
            try:
                # ASPX/SharePoint SitePage content fallback
                raw_html = file_path.read_text(encoding="utf-8", errors="ignore")
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(raw_html, "html.parser")
                
                # Remove unneeded elements
                for element in soup(["script", "style", "head", "title", "meta"]):
                    element.extract()
                    
                # Get clean, formatted text
                text = soup.get_text(separator="\n")
                lines = (line.strip() for line in text.splitlines())
                chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                content = "\n".join(chunk for chunk in chunks if chunk)
                
                if not content:
                    content = f"Empty SharePoint SitePage or raw ASPX: {file_path.name}"
            except Exception as e:
                content = f"Failed to parse ASPX page locally: {e}"
                
    # 3. Append captioned embedded images to markdown
    if extracted_images:
        content += "\n\n## Embedded Images\n\n"
        
        # Helper function to caption a single extracted image
        def _caption_extracted_image(item):
            safe_name, img_local_path = item
            caption = "Image extracted from attachment."
            
            if not img_local_path.exists():
                return safe_name, caption
                
            file_size = img_local_path.stat().st_size
            ext = os.path.splitext(safe_name)[1].lower()
            supported_formats = (".png", ".jpg", ".jpeg", ".webp")
            
            # 1. Skip tiny images (< 15KB) like icons/graphics
            if file_size < 15360:
                return safe_name, "Small icon/graphic from attachment."
                
            # 2. Skip unsupported formats which fail on Gemini API (like Windows EMF vector files)
            if ext not in supported_formats:
                return safe_name, f"Embedded {ext.strip('.').upper()} diagram."
                
            if gemini_client:
                print(f"     -> [Parallel] Captioning embedded image {safe_name} ({file_size/1024:.1f} KB)...")
                mime = "image/png" if ext == ".png" else "image/jpeg"
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
            
    return [("", content)], [item[0] for item in extracted_images]

# --- MSAL / Graph API Auth Handling ---

def get_msal_token() -> str | None:
    """Uses MSAL to acquire an OAuth token. Handles silent updates via local cache and device code flow."""
    import msal
    
    client_id = os.getenv("SHAREPOINT_CLIENT_ID")
    tenant_id = os.getenv("SHAREPOINT_TENANT_ID")
    
    client_secret = os.getenv("SHAREPOINT_CLIENT_SECRET")
    
    if not client_id or not tenant_id:
        print("  [Error] SHAREPOINT_CLIENT_ID or SHAREPOINT_TENANT_ID not set in .env. Skipping Graph API flow.")
        return None
        
    cache_path = workspace_dir / "sharepoint_token_cache.bin"
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        try:
            cache.deserialize(cache_path.read_text(encoding="utf-8"))
            print("  -> Loaded existing MSAL token cache.")
        except Exception as e:
            print(f"  [Warning] Failed loading token cache: {e}. Re-authenticating.")
            
    if client_secret:
        print("  -> SHAREPOINT_CLIENT_SECRET found. Using ConfidentialClientApplication (Client Credentials flow).")
        app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            token_cache=cache
        )
        # Use .default scope for client credentials / application-level permissions flow
        scopes = ["https://graph.microsoft.com/.default"]
        
        print("  -> Attempting to acquire token silently from cache...")
        result = app.acquire_token_silent(scopes=scopes, account=None)
        if not result:
            print("  -> Cache miss or expired. Acquiring token via client credentials flow...")
            try:
                result = app.acquire_token_for_client(scopes=scopes)
            except Exception as e:
                print(f"  [Error] Confidential client login failed: {e}")
                return None
    else:
        print("  -> No SHAREPOINT_CLIENT_SECRET found. Using PublicClientApplication (Device Code flow).")
        app = msal.PublicClientApplication(
            client_id=client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            token_cache=cache
        )
        
        scopes = ["Files.Read.All", "Sites.Read.All", "User.Read"]
        accounts = app.get_accounts()
        result = None
        
        if accounts:
            print("  -> Attempting to acquire token silently from cache...")
            result = app.acquire_token_silent(scopes=scopes, account=accounts[0])
            
        if not result:
            print("  -> No cached token found or cache expired. Starting interactive Device Code Flow...")
            if not sys.stdin.isatty():
                print("  [Error] Standard input is not a TTY. Cannot prompt user interactively in headless execution.")
                return None
                
            try:
                flow = app.initiate_device_flow(scopes=scopes)
                if "message" not in flow:
                    print(f"  [Error] Could not initiate device flow: {flow}")
                    return None
                    
                print("\n" + "="*80)
                print(flow["message"])
                print("="*80 + "\n")
                
                result = app.acquire_token_by_device_flow(flow)
            except Exception as e:
                print(f"  [Error] Device code login failed: {e}")
                return None
                
    if result and "access_token" in result:
        # Save token cache state
        if cache.has_state_changed:
            try:
                cache_path.write_text(cache.serialize(), encoding="utf-8")
                print("  -> Saved updated MSAL token cache.")
            except Exception as e:
                print(f"  [Warning] Failed to write token cache file: {e}")
        return result["access_token"]
    else:
        print(f"  [Error] Failed to acquire token: {result.get('error_description', result.get('error', 'Unknown error'))}")
        return None

# --- Ingestion States (GCS synchronization) ---

def fetch_sharepoint_item_permissions(base_api: str, drive_id: str, item_id: str, headers: dict | None) -> dict:
    """Fetches read permissions/restrictions for a SharePoint item via Graph REST API.
    Returns a dict with: {'restricted': bool, 'allowed_users': list[str], 'allowed_groups': list[str]}"""
    try:
        url = f"{base_api}/drives/{drive_id}/items/{item_id}/permissions"
        resp = httpx.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("value", [])
            
            allowed_users = []
            allowed_groups = []
            has_explicit_permission = False
            
            for perm in results:
                # If inheritedFrom is absent, it means this permission is explicitly configured (not inherited)
                # indicating a restricted file or custom permission.
                inherited_from = perm.get("inheritedFrom")
                roles = perm.get("roles", [])
                
                # We care about Read / Write / Owner roles
                has_read = any(r in ("read", "write", "owner") for r in roles)
                if not has_read:
                    continue
                
                # Check for explicit (restricted) permissions
                if not inherited_from:
                    has_explicit_permission = True
                
                # Gather users
                user_info = perm.get("grantedTo", {}).get("user", {})
                email = user_info.get("email") or user_info.get("displayName")
                if email:
                    allowed_users.append(email.lower())
                
                # Gather groups
                group_info = perm.get("grantedTo", {}).get("group", {})
                group_name = group_info.get("displayName") or group_info.get("email")
                if group_name:
                    allowed_groups.append(group_name.lower())
                
                # Gather identities (identitiesV2 has multiple entries sometimes)
                for identity in perm.get("grantedToIdentitiesV2", []):
                    u_info = identity.get("user", {})
                    u_email = u_info.get("email") or u_info.get("displayName")
                    if u_email:
                        allowed_users.append(u_email.lower())
                    g_info = identity.get("group", {})
                    g_name = g_info.get("displayName") or g_info.get("email")
                    if g_name:
                        allowed_groups.append(g_name.lower())

            # De-duplicate
            allowed_users = list(set(allowed_users))
            allowed_groups = list(set(allowed_groups))
            
            # Restricted if actual explicit entries are configured on this item
            restricted = has_explicit_permission and (len(allowed_users) > 0 or len(allowed_groups) > 0)
            return {
                "restricted": restricted,
                "allowed_users": allowed_users,
                "allowed_groups": allowed_groups
            }
    except Exception as e:
        print(f"     [Warning] Failed fetching permissions for SharePoint item {item_id}: {e}")
    return {"restricted": False, "allowed_users": [], "allowed_groups": []}

def download_state_from_gcs(bucket_name: str, local_catalog_path: Path, local_map_path: Path, local_perm_path: Path = None):
    """Downloads catalog and mapping from GCS to local paths if they exist."""
    print("  -> Attempting to download SharePoint ingestion state from GCS...")
    try:
        download_file_from_gcs(f"gs://{bucket_name}/sharepoint_catalog.json", str(local_catalog_path))
        download_file_from_gcs(f"gs://{bucket_name}/gcs_sharepoint_map.json", str(local_map_path))
        if local_perm_path:
            download_file_from_gcs(f"gs://{bucket_name}/gcs_sharepoint_permissions_map.json", str(local_perm_path))
        
        if local_catalog_path.exists():
            print(f"     -> Successfully downloaded sharepoint_catalog.json from gs://{bucket_name}")
        if local_map_path.exists():
            print(f"     -> Successfully downloaded gcs_sharepoint_map.json from gs://{bucket_name}")
        if local_perm_path and local_perm_path.exists():
            print(f"     -> Successfully downloaded gcs_sharepoint_permissions_map.json from gs://{bucket_name}")
    except Exception as e:
        print(f"     [Info] No existing SharePoint state found on GCS or error downloading: {e}")

def upload_state_to_gcs(bucket_name: str, local_catalog_path: Path, local_map_path: Path, local_perm_path: Path = None):
    """Uploads the updated catalog and mapping back to GCS."""
    print("  -> Uploading updated SharePoint ingestion state to GCS...")
    try:
        if local_catalog_path.exists():
            if upload_file_to_gcs(local_catalog_path, f"gs://{bucket_name}/sharepoint_catalog.json"):
                print(f"     -> Uploaded sharepoint_catalog.json to gs://{bucket_name}")
        if local_map_path.exists():
            if upload_file_to_gcs(local_map_path, f"gs://{bucket_name}/gcs_sharepoint_map.json"):
                print(f"     -> Uploaded gcs_sharepoint_map.json to gs://{bucket_name}")
        if local_perm_path and local_perm_path.exists():
            if upload_file_to_gcs(local_perm_path, f"gs://{bucket_name}/gcs_sharepoint_permissions_map.json"):
                print(f"     -> Uploaded gcs_sharepoint_permissions_map.json to gs://{bucket_name}")
    except Exception as e:
        print(f"     [Warning] Failed to upload state to GCS: {e}")

# --- Core Crawler Functions ---

def parse_aspx_page_json(page_json: dict) -> str:
    """Helper to convert SharePoint sitePage JSON into structured markdown."""
    title = page_json.get("title", "Untitled SharePoint Page")
    md_out = [f"# {title}\n"]
    
    # Try to unpack page canvas components
    canvas = page_json.get("canvas", {})
    web_parts = canvas.get("webParts", [])
    
    for wp in web_parts:
        # Modern text web part
        if wp.get("type") == "text":
            html_val = wp.get("data", {}).get("innerHTML", "")
            if html_val:
                # Strip and clean HTML
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html_val, "html.parser")
                # Clean links and headers
                cleaned_text = soup.get_text(separator=" ", strip=True)
                md_out.append(cleaned_text + "\n")
        # Image web part
        elif wp.get("type") == "image":
            img_src = wp.get("data", {}).get("imageSource", "")
            img_alt = wp.get("data", {}).get("alternativeText", "Image")
            if img_src:
                md_out.append(f"![{img_alt}]({img_src})\n")
                
    return "\n".join(md_out)

def process_file_item(
    item_details: dict,
    download_fn, # callable that downloads the file and returns local path
    gemini_client,
    temp_images_dir: Path,
    catalog: dict,
    force_reingest: bool,
    source_system: str = "SharePoint",
    headers: dict = None,
) -> tuple[list[dict], tuple[str, dict] | None]:
    """Universal parser executor for a single SharePoint file. Works for local sync and Graph API downloads."""
    item_id = item_details["id"]
    name = item_details["name"]
    last_mod = item_details["last_modified"] # ISO timestamp string
    url = item_details["url"]
    
    ext = os.path.splitext(name)[1].lower()
    supported_exts = (".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".py", ".drawio", ".vsdx", ".png", ".jpg", ".aspx")
    
    if ext not in supported_exts:
        return [], None
        
    # Retrieve permissions
    real_drive_id = item_details.get("real_drive_id")
    real_item_id = item_details.get("real_item_id")
    permissions = {"restricted": False, "allowed_users": [], "allowed_groups": []}
    if headers and real_drive_id and real_item_id:
        permissions = fetch_sharepoint_item_permissions("https://graph.microsoft.com/v1.0", real_drive_id, real_item_id, headers)
    
    # Enrich permissions dictionary with site metadata
    site_name = item_details.get("site_name", "")
    permissions = permissions.copy()
    permissions["site_name"] = site_name
        
    catalog_key = f"sharepoint_{item_id}"
    
    # Check catalog cache
    if not force_reingest and catalog_key in catalog:
        cached = catalog[catalog_key]
        if cached.get("last_modified") == last_mod:
            return [], None
            
    site_name = item_details.get("site_name", "")
    site_info = f" [Site: {site_name}]" if site_name else ""
    print(f"  [Ingest] Processing modified/new SharePoint file: {name}{site_info}")
    
    bucket_name = os.getenv("RAG_GCS_BUCKET_NAME")
    if not bucket_name:
        bucket_name = "confluence-sharepoint-rag-bucket"
        
    docs = []
    
    # Create thread-safe, isolated subdirectory for this file to prevent filename/temp conflicts
    file_temp_dir = temp_images_dir / f"file_{item_id}"
    file_temp_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Ingest standalone image files (.png, .jpg)
        if ext in (".png", ".jpg"):
            local_path = download_fn()
            if not local_path or not local_path.exists():
                raise RuntimeError("Download failed - check crawl logs for detailed network errors.")
                
            try:
                img_bytes = local_path.read_bytes()
                # Copy to images dir directly
                safe_name = re.sub(r'[\\/*?:"<>|\'`]', "", name).replace(" ", "_")
                dest_path = file_temp_dir / safe_name
                shutil.copy(local_path, dest_path)
                
                caption = "Image file."
                if gemini_client:
                    print(f"     -> Captioning standalone image: {name}")
                    mime = "image/png" if ext == ".png" else "image/jpeg"
                    caption = describe_image_with_gemini(gemini_client, img_bytes, mime)
                    
                # Upload standalone image to GCS immediately
                try:
                    if upload_file_to_gcs(dest_path, f"gs://{bucket_name}/images/{safe_name}"):
                        print(f"     -> [GCS] Uploaded standalone image: {safe_name}")
                except Exception as ex:
                    print(f"     [Warning] Failed uploading image {safe_name}: {ex}")
                    
                # Clean up copied image
                if dest_path.exists():
                    dest_path.unlink()
                    
                doc_content = (
                    f"![{name}](https://storage.googleapis.com/{bucket_name}/images/{safe_name})\n"
                    f"[IMAGE CAPTION: {caption}]\n"
                )
                
                docs = [{
                    "id": catalog_key,
                    "title": name,
                    "content": doc_content,
                    "source": source_system,
                    "url": url,
                    "restricted": permissions.get("restricted", False),
                    "allowed_users": permissions.get("allowed_users", []),
                    "allowed_groups": permissions.get("allowed_groups", []),
                    "site_name": permissions.get("site_name", ""),
                }]
                
                # Immediately upload the markdown for this standalone image
                filename = clean_filename(name)
                filepath = file_temp_dir / filename
                
                restricted_str = "True" if permissions.get("restricted") else "False"
                allowed_groups_str = ",".join(permissions.get("allowed_groups", []))
                allowed_users_str = ",".join(permissions.get("allowed_users", []))

                enriched = (
                    f"source_url: {url}\n"
                    f"title: {name}\n"
                    f"source_system: {source_system}\n"
                    f"restricted: {restricted_str}\n"
                    f"allowed_groups: {allowed_groups_str}\n"
                    f"allowed_users: {allowed_users_str}\n\n"
                    f"{doc_content}\n\n"
                    f"---\n"
                    f"doc_id: {catalog_key}\n"
                    f"source_url: {url}\n"
                )
                filepath.write_text(enriched, encoding="utf-8")
                
                try:
                    if upload_file_to_gcs(filepath, f"gs://{bucket_name}/{filename}"):
                        print(f"     -> [GCS] Uploaded markdown: {filename}")
                except Exception as e:
                    print(f"     [Warning] Failed uploading markdown {filename}: {e}")
                    
                if filepath.exists():
                    filepath.unlink()
                    
            finally:
                # Clean up the downloaded local file if it's a temporary download
                if "graph_downloads" in str(local_path):
                    try:
                        if local_path.exists():
                            local_path.unlink()
                            print(f"     -> Deleted temporary download: {local_path.name}")
                    except Exception as e:
                        print(f"     [Warning] Failed to delete temporary download {local_path.name}: {e}")
                        
        else:
            # Standard Document
            local_path = download_fn()
            if not local_path or not local_path.exists():
                raise RuntimeError("Download failed - check crawl logs for detailed network errors.")
                
            # Hard fail-safe check on size of downloaded file to prevent in-memory tmpfs OOM
            file_size = local_path.stat().st_size
            max_size_mb = int(os.getenv("SHAREPOINT_MAX_FILE_SIZE_MB", "500"))
            max_size_bytes = max_size_mb * 1024 * 1024
            if file_size > max_size_bytes:
                print(f"     [Warning] Skipping large standard file {name} ({file_size / (1024*1024):.2f} MB) - Max limit: {max_size_mb} MB")
                try:
                    if "graph_downloads" in str(local_path) and local_path.exists():
                        local_path.unlink()
                except Exception as unlink_err:
                    pass
                return [], None
                
            try:
                # Parse content
                content_parts, extracted_image_names = parse_attachment_to_markdown(
                    local_path,
                    gemini_client,
                    file_temp_dir,
                    bucket_name
                )
                
                # Upload extracted images to GCS and delete them immediately
                for safe_name in extracted_image_names:
                    img_path = file_temp_dir / safe_name
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
                
                docs = []
                filepath = None
                for part_suffix, part_content in content_parts:
                    part_catalog_key = catalog_key
                    part_name = name
                    if part_suffix:
                        safe_suffix = part_suffix.lower().replace(" ", "_").replace("-", "_").strip("_")
                        part_catalog_key = f"{catalog_key}_{safe_suffix}"
                        part_name = f"{name}{part_suffix}"
                    
                    # Rewrite relative image references to public GCS bucket paths
                    part_content = re.sub(
                        r'!\[([^\]]*)\]\(images/([^)]+)\)',
                        lambda m: f"![{m.group(1)}](https://storage.googleapis.com/{bucket_name}/images/{m.group(2)})",
                        part_content
                    )
                    
                    docs.append({
                        "id": part_catalog_key,
                        "title": part_name,
                        "content": "",  # Save memory
                        "source": source_system,
                        "url": url,
                        "restricted": permissions.get("restricted", False),
                        "allowed_users": permissions.get("allowed_users", []),
                        "allowed_groups": permissions.get("allowed_groups", []),
                        "site_name": permissions.get("site_name", ""),
                    })
                    
                    # Immediately upload markdown to GCS
                    filename = clean_filename(part_name)
                    if not filename.lower().endswith(".md"):
                        filename = filename + ".md"
                    filepath = file_temp_dir / filename
                    
                    restricted_str = "True" if permissions.get("restricted") else "False"
                    allowed_groups_str = ",".join(permissions.get("allowed_groups", []))
                    allowed_users_str = ",".join(permissions.get("allowed_users", []))

                    enriched = (
                        f"source_url: {url}\n"
                        f"title: {part_name}\n"
                        f"source_system: {source_system}\n"
                        f"restricted: {restricted_str}\n"
                        f"allowed_groups: {allowed_groups_str}\n"
                        f"allowed_users: {allowed_users_str}\n\n"
                        f"{part_content}\n\n"
                        f"---\n"
                        f"doc_id: {part_catalog_key}\n"
                        f"source_url: {url}\n"
                    )
                    
                    try:
                        filepath.write_text(enriched, encoding="utf-8")
                        if upload_file_to_gcs(filepath, f"gs://{bucket_name}/{filename}"):
                            print(f"     -> [GCS] Uploaded markdown: {filename}")
                    except Exception as e:
                        print(f"     [Warning] Failed uploading markdown {filename}: {e}")
                    finally:
                        try:
                            if filepath and filepath.exists():
                                filepath.unlink()
                        except Exception as del_err:
                            pass
                    
            finally:
                # Clean up the downloaded local file if it's a temporary download
                if "graph_downloads" in str(local_path):
                    try:
                        if local_path.exists():
                            local_path.unlink()
                            print(f"     -> Deleted temporary download: {local_path.name}")
                    except Exception as e:
                        print(f"     [Warning] Failed to delete temporary download {local_path.name}: {e}")
                        
        catalog_update = (catalog_key, {
            "last_modified": last_mod,
            "title": name,
            "filename": clean_filename(name)
        })
        
        return docs, catalog_update

    finally:
        try:
            if file_temp_dir.exists():
                shutil.rmtree(file_temp_dir)
        except Exception as del_err:
            print(f"     [Warning] Failed to clean up file-specific temp directory {file_temp_dir.name}: {del_err}")

# --- Method A: Local Folder Crawler ---

def run_local_sync_crawl(
    sync_dir_str: str,
    gemini_client,
    temp_images_dir: Path,
    catalog: dict,
    gcs_map: dict,
    force_reingest: bool
) -> list:
    """Crawl a synced local folder, processes files, and sets up GCS markdown files."""
    sync_dir = Path(sync_dir_str)
    if not sync_dir.exists():
        print(f"  [Error] Synced folder path does not exist: {sync_dir}")
        return []
        
    print(f"  -> Crawling local sync directory: {sync_dir}")
    all_files = []
    # Enumerate all files in local directory recursively
    for path in sync_dir.rglob("*"):
        if path.is_file():
            # Skip hidden files or lock files
            if path.name.startswith("~") or path.name.startswith("."):
                continue
                
            # Skip unsupported extensions early during local crawl
            ext = path.suffix.lower()
            supported_exts = (".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".py", ".drawio", ".vsdx", ".png", ".jpg", ".aspx")
            if ext not in supported_exts:
                continue
                
            # Skip extremely large local files (default: 500MB)
            file_size = path.stat().st_size
            max_size_mb = int(os.getenv("SHAREPOINT_MAX_FILE_SIZE_MB", "500"))
            max_size_bytes = max_size_mb * 1024 * 1024
            if file_size > max_size_bytes:
                print(f"     [Info] Skipping large local file {path.name} ({file_size / (1024*1024):.2f} MB) - Max limit: {max_size_mb} MB")
                continue
                
            all_files.append(path)
            
    print(f"  -> Found {len(all_files)} total file(s) in sync folder after filtering.")
    
    docs = []
    processed_count = 0
    skipped_count = 0
    
    # Process sequentially locally to keep console clean
    for path in all_files:
        try:
            # We generate a unique ID using the relative path hash
            import hashlib
            rel_path = str(path.relative_to(sync_dir))
            item_id = hashlib.md5(rel_path.encode('utf-8')).hexdigest()
            
            # Form mock local URL or file path URL
            file_url = f"file:///{path.as_posix()}"
            
            item_details = {
                "id": item_id,
                "name": path.name,
                "last_modified": str(path.stat().st_mtime), # use file mtime string
                "url": file_url
            }
            
            def download_fn():
                return path # already downloaded
                
            res_docs, cat_update = process_file_item(
                item_details,
                download_fn,
                gemini_client,
                temp_images_dir,
                catalog,
                force_reingest
            )
            
            if cat_update:
                k, v = cat_update
                catalog[k] = v
                docs.extend(res_docs)
                processed_count += 1
                
                # Update GCS SharePoint Map in real-time
                for doc in res_docs:
                    filename = clean_filename(doc["title"])
                    gcs_map[filename] = doc["url"]
                
                # Save incremental/intermittent state to GCS to safeguard against timeouts
                bucket_name = os.getenv("RAG_GCS_BUCKET_NAME")
                if bucket_name and bucket_name != "YOUR_BUCKET_NAME":
                    try:
                        catalog_filepath = Path(__file__).parent / "sharepoint_catalog.json"
                        map_filepath = Path(__file__).parent / "gcs_sharepoint_map.json"
                        with open(catalog_filepath, "w", encoding="utf-8") as f:
                            json.dump(catalog, f, indent=2)
                        with open(map_filepath, "w", encoding="utf-8") as f:
                            json.dump(gcs_map, f, indent=2)
                        upload_state_to_gcs(bucket_name, catalog_filepath, map_filepath)
                        print(f"     -> [GCS] Intermittent progress uploaded successfully (processed {processed_count} files).")
                    except Exception as state_err:
                        print(f"     [Warning] Failed uploading intermittent progress: {state_err}")
            else:
                skipped_count += 1
        except Exception as e:
            print(f"  [Error] Failed to process file {path.name}: {e}")
            
    print(f"     Processed (modified or new):   {processed_count}")
    print(f"     Skipped (cached):              {skipped_count}")
    return docs

# --- Method B: Graph API Parallel Crawler ---

def fetch_graph_api_data(
    token: str,
    gemini_client,
    temp_images_dir: Path,
    catalog: dict,
    gcs_map: dict,
    force_reingest: bool
) -> list:
    """Queries Graph API to discover sites, list libraries, download and parse files in parallel."""
    headers = {"Authorization": f"Bearer {token}"}
    base_url = "https://graph.microsoft.com/v1.0"
    my_tenant = "your-organization.sharepoint.com"
    my_site = "/sites/YourApplicationSite"

    # 1. Discover SharePoint sites via Graph API
    print("  -> Discovering SharePoint sites via Graph API...")
    sites = []
    
    # Check if a whitelist/filter is provided in .env
    site_filter = [s.strip() for s in os.getenv("SHAREPOINT_SITES_LIST", "").split(",") if s.strip()]
    env_my_site = os.getenv("SHAREPOINT_SINGLE_SITE_PATH")
    
    try:
        if env_my_site:
            print(f"  -> Explicit single site path '{env_my_site}' requested.")
            url = f"{base_url}/sites/{my_tenant}:{env_my_site}"
            resp = httpx.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            sites = [resp.json()]
        elif site_filter:
            print(f"  -> Fetching specified sites directly from whitelist to avoid global search: {site_filter}")
            for site_name_or_path in site_filter:
                try:
                    # Support both "/sites/Name" and "Name"
                    site_path = site_name_or_path if site_name_or_path.startswith("/") else f"/sites/{site_name_or_path}"
                    url = f"{base_url}/sites/{my_tenant}:{site_path}"
                    resp = httpx.get(url, headers=headers, timeout=60)
                    resp.raise_for_status()
                    sites.append(resp.json())
                except Exception as site_err:
                    print(f"  [Warning] Could not fetch site '{site_name_or_path}' directly: {site_err}")
                    sharepoint_failures.append({
                        "file_name": f"Site_{site_name_or_path}",
                        "error_code": type(site_err).__name__,
                        "failure_reason": f"Could not fetch site directly: {str(site_err)}",
                        "site_name": site_name_or_path
                    })
        else:
            # Fallback to search discovery to find all accessible sites
            search_query = os.getenv("SHAREPOINT_SITE_SEARCH_QUERY", "").strip()
            # If search query is empty/unset, use '*' to discover all sites
            if not search_query:
                search_query = "*"
            print(f"  -> Searching sites with query: '{search_query}'")
            url = f"{base_url}/sites?search={search_query}"
            resp = httpx.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            sites = resp.json().get("value", [])

        print(f"  -> Discovered {len(sites)} SharePoint sites.")
    except Exception as e:
        print(f"  [Error] Failed discovering sites: {e}")
        if "403" in str(e):
            print("  [Tip] 403 Forbidden on global search usually means your Graph API registration lacks tenant-wide 'Sites.Read.All' search permissions.")
            print("        To crawl specific sites, please list them directly in the 'SHAREPOINT_SITES_LIST' environment variable (e.g. YourApplicationSite).")
            print("        This bypasses global search and fetches those specific sites directly by path.")
        return []
        
    # Check if a whitelist/filter is provided in .env
    if site_filter:
        normalized_filter = []
        for s in site_filter:
            normalized_filter.append(s)
            if s.startswith("/sites/"):
                normalized_filter.append(s.replace("/sites/", ""))
            normalized_filter.append(s.split("/")[-1])
            
        sites = [s for s in sites if s.get("name") in normalized_filter or s.get("displayName") in normalized_filter]
        print(f"  -> Filtered sites list to: {[s.get('name') for s in sites]}")
        
    # Enumerate all files to crawl across all sites
    all_items_to_crawl = []
    
    # Local downloader function factory
    def make_download_fn(download_url: str, content_url: str, headers_snapshot: dict, temp_path: Path):
        def fn():
            try:
                max_size_mb = int(os.getenv("SHAREPOINT_MAX_FILE_SIZE_MB", "500"))
                max_size_bytes = max_size_mb * 1024 * 1024

                # Helper to perform stream download with limit check
                def stream_download(url, use_headers=False):
                    req_headers = None
                    if use_headers:
                        # Dynamically acquire a fresh access token to prevent 401 expiration errors during long crawls
                        fresh_token = get_msal_token()
                        if fresh_token:
                            req_headers = {"Authorization": f"Bearer {fresh_token}"}
                        else:
                            req_headers = headers_snapshot
                    with httpx.stream("GET", url, headers=req_headers, follow_redirects=True, timeout=120) as r:
                        r.raise_for_status()
                        
                        # Content-Length check
                        content_length = r.headers.get("Content-Length")
                        if content_length:
                            try:
                                if int(content_length) > max_size_bytes:
                                    print(f"     [Warning] Skipping large SharePoint file {temp_path.name} ({int(content_length) / (1024*1024):.2f} MB) via Content-Length header - Max limit: {max_size_mb} MB")
                                    return False, True # (success=False, limit_exceeded=True)
                            except ValueError:
                                pass
                        
                        temp_path.parent.mkdir(parents=True, exist_ok=True)
                        bytes_downloaded = 0
                        with open(temp_path, "wb") as f:
                            for chunk in r.iter_bytes(chunk_size=8192):
                                bytes_downloaded += len(chunk)
                                if bytes_downloaded > max_size_bytes:
                                    print(f"     [Warning] Skipping large SharePoint file {temp_path.name} (> {max_size_mb} MB) - Max limit reached during download. Aborting.")
                                    f.close()
                                    if temp_path.exists():
                                        temp_path.unlink()
                                    return False, True
                                f.write(chunk)
                        return True, False

                # 1. Try pre-signed downloadUrl (unauthenticated)
                if download_url:
                    print(f"     [Debug] Downloading {temp_path.name} via downloadUrl...")
                    try:
                        success, limit_exceeded = stream_download(download_url, use_headers=False)
                        if limit_exceeded:
                            return None
                        if success and temp_path.exists() and temp_path.stat().st_size > 0:
                            return temp_path
                        else:
                            print(f"     [Info] Pre-signed downloadUrl for {temp_path.name} returned 0 bytes or failed. Trying /content fallback...")
                    except Exception as pre_err:
                        print(f"     [Warning] Pre-signed downloadUrl failed for {temp_path.name} ({pre_err}). Trying /content fallback...")

                # 2. Try authenticated Graph API /content endpoint fallback
                if content_url:
                    print(f"     [Debug] Downloading {temp_path.name} via /content...")
                    success, limit_exceeded = stream_download(content_url, use_headers=True)
                    if limit_exceeded:
                        return None
                    if success and temp_path.exists() and temp_path.stat().st_size > 0:
                        return temp_path

                return None
            except Exception as dl_err:
                print(f"     [Warning] Download failed for {temp_path.name}: {dl_err}")
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except Exception:
                        pass
                return None
        return fn

    # Discover libraries and recursively list files per site
    raw_downloads_dir = temp_images_dir.parent / "graph_downloads"
    raw_downloads_dir.mkdir(exist_ok=True)
    
    print("  -> Crawling Document Libraries for all sites...")
    for site in sites:
        site_id = site.get("id")
        site_name = site.get("name", "UnknownSite")
        print(f"     -> Crawling site: '{site_name}'...")
        
        # Get drives (Document Libraries)
        try:
            drives_url = f"{base_url}/sites/{site_id}/drives"
            d_resp = httpx.get(drives_url, headers=headers, timeout=30)
            d_resp.raise_for_status()
            drives = d_resp.json().get("value", [])
            
            for drive in drives:
                drive_id = drive.get("id")
                
                # Recursive traversal queue (contains folder item IDs or paths)
                folder_queue = [("root", "")] # tuple of (folder_id, relative_path)
                
                while folder_queue:
                    folder_id, rel_path = folder_queue.pop(0)
                    
                    if folder_id == "root":
                        children_url = f"{base_url}/drives/{drive_id}/root/children"
                    else:
                        children_url = f"{base_url}/drives/{drive_id}/items/{folder_id}/children"
                        
                    try:
                        # Listing folder children with robust exponential backoff retries & longer timeout
                        children = []
                        retries = 3
                        backoff = 2
                        import time
                        for attempt in range(retries):
                            try:
                                c_resp = httpx.get(children_url, headers=headers, timeout=90)
                                c_resp.raise_for_status()
                                children = c_resp.json().get("value", [])
                                break # Success! Break retry loop
                            except httpx.HTTPStatusError as http_err:
                                if http_err.response.status_code == 429:
                                    retry_after = int(http_err.response.headers.get("Retry-After", backoff * (attempt + 1)))
                                    print(f"     [Warning] Rate-limited (HTTP 429) listing folder {folder_id}. Retrying after {retry_after}s...")
                                    time.sleep(retry_after)
                                else:
                                    if attempt == retries - 1:
                                        raise
                                    time.sleep(backoff ** attempt)
                            except (httpx.ReadTimeout, httpx.ConnectTimeout) as timeout_err:
                                print(f"     [Warning] Timeout listing folder {folder_id} (Attempt {attempt+1}/{retries}): {timeout_err}")
                                if attempt == retries - 1:
                                    raise
                                time.sleep(backoff ** attempt)
                            except Exception as other_err:
                                if attempt == retries - 1:
                                        raise
                                time.sleep(backoff ** attempt)
                        
                        for item in children:
                            item_name = item.get("name")
                            item_id = item.get("id")
                            
                            # Subfolder
                            if "folder" in item:
                                folder_queue.append((item_id, f"{rel_path}/{item_name}"))
                            # File
                            elif "file" in item:
                                download_url = item.get("@microsoft.graph.downloadUrl")
                                last_modified = item.get("lastModifiedDateTime", "")
                                web_url = item.get("webUrl", "")
                                file_size = item.get("size", 0)
                                
                                # Skip unsupported extensions early during crawl
                                ext = os.path.splitext(item_name)[1].lower()
                                supported_exts = (".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".py", ".drawio", ".vsdx", ".png", ".jpg", ".aspx")
                                if ext not in supported_exts:
                                    continue
                                    
                                # Skip extremely large files (default: 500MB) to prevent container OOM (exit code 137)
                                max_size_mb = int(os.getenv("SHAREPOINT_MAX_FILE_SIZE_MB", "500"))
                                max_size_bytes = max_size_mb * 1024 * 1024
                                if file_size > max_size_bytes:
                                    print(f"     [Info] Skipping large file {item_name} ({file_size / (1024*1024):.2f} MB) - Max limit: {max_size_mb} MB")
                                    continue
                                
                                if download_url:
                                    import hashlib
                                    # Create a short 8-char hash of the drive/item to ensure uniqueness
                                    unique_hash = hashlib.md5(f"{drive_id}_{item_id}".encode("utf-8")).hexdigest()[:8]
                                    # Create a clean, human-readable name prefix (allowing only alphanumeric, underscores, and dashes)
                                    clean_name = re.sub(r'[\\/*?:"<>|\'` ]', "_", os.path.splitext(item_name)[0])
                                    # Remove any characters that are not alphanumeric, underscores, or dashes (like exclamation marks)
                                    clean_name = re.sub(r'[^a-zA-Z0-9_-]', '', clean_name)
                                    # Clean up multiple consecutive underscores
                                    clean_name = re.sub(r'_+', '_', clean_name).strip("_")
                                    
                                    if not clean_name:
                                        clean_name = "sharepoint_doc"
                                        
                                    safe_id = f"{clean_name}_{unique_hash}"
                                    temp_file_path = raw_downloads_dir / f"{safe_id}{os.path.splitext(item_name)[1]}"
                                    content_url = f"{base_url}/drives/{drive_id}/items/{item_id}/content"
                                    
                                    all_items_to_crawl.append({
                                        "item_details": {
                                            "id": safe_id,
                                            "name": item_name,
                                            "last_modified": last_modified,
                                            "url": web_url,
                                            "size": file_size,
                                            "site_name": site_name,
                                            "real_drive_id": drive_id,
                                            "real_item_id": item_id
                                        },
                                        "download_fn": make_download_fn(download_url, content_url, headers, temp_file_path)
                                    })
                    except Exception as child_err:
                        print(f"     [Warning] Failed listing folder {folder_id} in drive {drive_id}: {child_err}")
                        
        except Exception as drive_err:
            print(f"     [Warning] Failed loading libraries for site {site_name}: {drive_err}")
            
    print(f"  -> Discovered {len(all_items_to_crawl)} total file(s) across all SharePoint libraries.")
    
    # 2. Run multi-threaded ingestion
    concurrency = int(os.getenv("SHAREPOINT_INGEST_CONCURRENCY", "3"))
    print(f"  -> Launching parallel parser crawl with {concurrency} threads...")
    
    docs = []
    processed_count = 0
    skipped_count = 0
    import time
    last_gcs_upload_time = time.time()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                process_file_item,
                item["item_details"],
                item["download_fn"],
                gemini_client,
                temp_images_dir,
                catalog,
                force_reingest,
                "SharePoint",
                headers
            ): item for item in all_items_to_crawl
        }
        
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                res_docs, cat_update = future.result()
                if cat_update:
                    k, v = cat_update
                    catalog[k] = v
                    docs.extend(res_docs)
                    processed_count += 1
                    
                    # Update GCS SharePoint Map and permissions map in real-time
                    for doc in res_docs:
                        filename = clean_filename(doc["title"])
                        gcs_map[filename] = doc["url"]
                        if "restricted" in doc:
                            gcs_permissions_map[filename] = {
                                "restricted": doc["restricted"],
                                "allowed_users": doc["allowed_users"],
                                "allowed_groups": doc["allowed_groups"],
                                "site_name": doc.get("site_name", "")
                            }
                    
                    # Save incremental/intermittent state to GCS to safeguard against timeouts
                    # To avoid excessive process-spawning, throttle GCS uploads to at most once every 30s or 10 files.
                    bucket_name = os.getenv("RAG_GCS_BUCKET_NAME")
                    if bucket_name and bucket_name != "YOUR_BUCKET_NAME":
                        current_time = time.time()
                        try:
                            catalog_filepath = Path(__file__).parent / "sharepoint_catalog.json"
                            map_filepath = Path(__file__).parent / "gcs_sharepoint_map.json"
                            perm_filepath = Path(__file__).parent / "gcs_sharepoint_permissions_map.json"
                            with open(catalog_filepath, "w", encoding="utf-8") as f:
                                json.dump(catalog, f, indent=2)
                            with open(map_filepath, "w", encoding="utf-8") as f:
                                json.dump(gcs_map, f, indent=2)
                            with open(perm_filepath, "w", encoding="utf-8") as f:
                                json.dump(gcs_permissions_map, f, indent=2)
                                
                            if (current_time - last_gcs_upload_time >= 30.0) or (processed_count % 10 == 0):
                                upload_state_to_gcs(bucket_name, catalog_filepath, map_filepath, perm_filepath)
                                last_gcs_upload_time = current_time
                                print(f"     -> [GCS] Intermittent progress uploaded successfully (processed {processed_count} files).")
                        except Exception as state_err:
                            print(f"     [Warning] Failed saving intermittent progress: {state_err}")
                else:
                    skipped_count += 1
            except Exception as e:
                print(f"  [Error] Parallel worker failed for item {item['item_details']['name']}: {e}")
                with state_lock:
                    sharepoint_failures.append({
                        "file_name": item["item_details"].get("name", "Unknown"),
                        "error_code": type(e).__name__,
                        "failure_reason": str(e),
                        "site_name": item["item_details"].get("site_name", "UNKNOWN")
                    })
                
    print(f"     Processed (modified or new):   {processed_count}")
    print(f"     Skipped (cached):              {skipped_count}")
    return docs

# --- Main Ingestion Logic ---

def main():
    print("=" * 80)
    print("RUNNING SHAREPOINT EXTRACTION, PARSING & GCS SYNC")
    print("=" * 80)
    
    bucket_name = os.getenv("RAG_GCS_BUCKET_NAME")
    if not bucket_name:
        print("Warning: RAG_GCS_BUCKET_NAME is not set. Using default.")
        bucket_name = "confluence-sharepoint-rag-bucket"
        
    temp_dir = workspace_dir / "gcs_sharepoint_temp"
    temp_images_dir = temp_dir / "images"
    
    # 1. Recreate clean local directories
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_images_dir.mkdir(parents=True, exist_ok=True)
    
    # 2. Ingestion catalogs
    catalog_filepath = Path(__file__).parent / "sharepoint_catalog.json"
    map_filepath = Path(__file__).parent / "gcs_sharepoint_map.json"
    perm_filepath = Path(__file__).parent / "gcs_sharepoint_permissions_map.json"
    
    # Download state
    download_state_from_gcs(bucket_name, catalog_filepath, map_filepath, perm_filepath)
    
    catalog = {}
    if catalog_filepath.exists():
        try:
            catalog = json.loads(catalog_filepath.read_text(encoding="utf-8"))
            print(f"  -> Loaded existing catalog with {len(catalog)} entry(s).")
        except Exception as e:
            print(f"  [Warning] Failed loading catalog: {e}. Starting fresh.")
            
    gcs_sharepoint_map = {}
    if map_filepath.exists():
        try:
            gcs_sharepoint_map = json.loads(map_filepath.read_text(encoding="utf-8"))
            print(f"  -> Loaded GCS-SharePoint URL mapping with {len(gcs_sharepoint_map)} entry(s).")
        except Exception as e:
            print(f"  [Warning] Failed loading URL mapping: {e}. Starting fresh.")
            
    # Load Permissions Map
    global gcs_permissions_map
    gcs_permissions_map = {}
    if perm_filepath.exists():
        try:
            gcs_permissions_map = json.loads(perm_filepath.read_text(encoding="utf-8"))
            print(f"  -> Loaded existing GCS Permissions Map with {len(gcs_permissions_map)} entry(s).")
        except Exception as e:
            print(f"  [Warning] Failed loading GCS permissions map: {e}. Starting fresh.")
            
    # 3. Gemini Client Setup
    try:
        client = get_gemini_client()
        has_gemini = True
        print("  -> Initialized Google GenAI SDK for captioning/parsing.")
    except Exception as e:
        print(f"  [Warning] Could not initialize Google GenAI client: {e}. Falling back to local parsers.")
        client = None
        has_gemini = False
        
    # Determine ingestion mode
    ingest_mode = os.getenv("SHAREPOINT_INGEST_MODE", "local_sync").lower()
    force_reingest = os.getenv("FORCE_REINGEST", "false").lower() == "true"
    
    docs = []
    
    if ingest_mode == "graph_api":
        print("  -> Ingestion Mode: Graph API (Method B)")
        token = get_msal_token()
        if token:
            docs = fetch_graph_api_data(
                token,
                client if has_gemini else None,
                temp_images_dir,
                catalog,
                gcs_sharepoint_map,
                force_reingest
            )
        else:
            print("  [Error] Graph API authentication failed. Ingestion bypassed.")
    else:
        print("  -> Ingestion Mode: Local Sync Crawler (Method A)")
        sync_dir = os.getenv("SHAREPOINT_LOCAL_SYNC_DIR")
        if sync_dir:
            docs = run_local_sync_crawl(
                sync_dir,
                client if has_gemini else None,
                temp_images_dir,
                catalog,
                gcs_sharepoint_map,
                force_reingest
            )
        else:
            print("  [Error] SHAREPOINT_LOCAL_SYNC_DIR is not configured in your .env file.")
            
    if docs:
        for doc in docs:
            filename = clean_filename(doc["title"])
            gcs_sharepoint_map[filename] = doc["url"]
            if "restricted" in doc:
                gcs_permissions_map[filename] = {
                    "restricted": doc["restricted"],
                    "allowed_users": doc["allowed_users"],
                    "allowed_groups": doc["allowed_groups"],
                    "site_name": doc.get("site_name", "")
                }
            
    # Save the updated files locally
    try:
        map_filepath.write_text(json.dumps(gcs_sharepoint_map, indent=2), encoding="utf-8")
        catalog_filepath.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        perm_filepath.write_text(json.dumps(gcs_permissions_map, indent=2), encoding="utf-8")
        print("Saved updated states locally.")
    except Exception as e:
        print(f"  [Warning] Failed writing state files locally: {e}")
        
    # Sync states back to GCS
    upload_state_to_gcs(bucket_name, catalog_filepath, map_filepath, perm_filepath)

    # Upload failure results report to GCS
    try:
        local_failures_path = Path(__file__).parent / "gcs_sharepoint_failure_results.json"
        local_failures_path.write_text(json.dumps(sharepoint_failures, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  -> Uploading {len(sharepoint_failures)} failure results to gs://{bucket_name}/gcs_sharepoint_failure_results.json...")
        upload_file_to_gcs(local_failures_path, f"gs://{bucket_name}/gcs_sharepoint_failure_results.json")
    except Exception as fail_err:
        print(f"  [Warning] Failed writing or uploading failure results: {fail_err}")

    # Clean up local folders
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
        print("Cleaned up temporary local folder.")

if __name__ == "__main__":
    main()


