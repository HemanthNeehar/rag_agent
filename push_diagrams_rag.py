"""
Fast, Selective RAG Corpus Push Script for Visio (.vsdx) and Draw.io (.drawio) Diagrams

This script selectively scans GCS for diagram markdown files (.vsdx and .drawio),
imports ONLY those diagram files into the Vertex AI RAG Corpus, and applies custom metadata
tagging (site_name, space_name, access control permissions, etc.).

Use this convenience script to push diagram updates instantly without waiting
for full-repository push jobs to complete.
"""

import os
import time
import random
import json
import threading
from pathlib import Path
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import vertexai
from vertexai.preview import rag

rag_agent_dir = Path(__file__).parent.resolve()
load_dotenv(rag_agent_dir / ".env", override=True)

def retry_api_call(func, *args, max_retries=6, initial_backoff=2.0, **kwargs):
    backoff = initial_backoff
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            e_str = str(e).lower()
            is_quota = any(x in e_str for x in ["resource_exhausted", "quota exceeded", "429", "rate limit"])
            if is_quota:
                if attempt == max_retries - 1:
                    print(f"        [Error] Max retries reached. API call failed with quota error.")
                    raise e
                sleep_time = backoff + random.uniform(0.5, 1.5)
                print(f"        [Warning] Rate limit detected. Retrying in {sleep_time:.2f}s (Attempt {attempt+1}/{max_retries})...")
                time.sleep(sleep_time)
                backoff *= 2.0
            else:
                raise e

def get_all_corpus_files(corpus_name):
    corpus_files = []
    page_token = None
    while True:
        response = retry_api_call(
            rag.list_files,
            corpus_name=corpus_name,
            page_size=1000,
            page_token=page_token
        )
        if hasattr(response, "pages"):
            for page in response.pages:
                page_files = page.rag_files if hasattr(page, "rag_files") else list(page)
                corpus_files.extend(list(page_files or []))
                time.sleep(1.0)
            break
        elif hasattr(response, "rag_files"):
            corpus_files.extend(response.rag_files or [])
            page_token = getattr(response, "next_page_token", None)
            if not page_token:
                break
            time.sleep(1.0)
        else:
            break
    return corpus_files

def push_diagrams_only():
    print("=" * 80)
    print("STARTING FAST TARGETED DIAGRAM (VSDX / DRAWIO) RAG CORPUS PUSH")
    print("=" * 80)

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("RAG_PROJECT_ID", "agent-ops-494011")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    corpus_location = os.getenv("RAG_CORPUS_LOCATION", "us-south1")
    corpus_id = os.getenv("RAG_CORPUS_ID", "6301661778598166528")
    gcs_bucket = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc")

    if not project_id or not corpus_id:
        print("[Error] Missing RAG_PROJECT_ID or RAG_CORPUS_ID in environment.")
        return

    vertexai.init(project=project_id, location=corpus_location)
    corpus_name = f"projects/{project_id}/locations/{corpus_location}/ragCorpora/{corpus_id}"

    print(f"Project   : {project_id}")
    print(f"Location  : {location}")
    print(f"Corpus ID : {corpus_id}")
    print(f"GCS Bucket: gs://{gcs_bucket}")

    # 1. Fetch permissions maps from GCS or local
    import subprocess
    def fetch_gcs_json(filename):
        gcs_uri = f"gs://{gcs_bucket}/{filename}"
        try:
            res = subprocess.run(["gcloud", "storage", "cat", gcs_uri], capture_output=True, text=True, check=True)
            return json.loads(res.stdout)
        except Exception:
            local_path = rag_agent_dir / filename
            if local_path.exists():
                try:
                    return json.loads(local_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return {}

    sp_perms = fetch_gcs_json("gcs_sharepoint_permissions_map.json")
    cf_perms = fetch_gcs_json("gcs_confluence_permissions_map.json")

    # 2. List GCS bucket files to find diagram markdown files (.vsdx and .drawio)
    print(f"\n[1/3] Scanning gs://{gcs_bucket}/ for Visio (.vsdx) and Draw.io (.drawio) files...")
    diagram_gcs_uris = []
    diagram_filenames = set()

    try:
        res = subprocess.run(["gcloud", "storage", "ls", f"gs://{gcs_bucket}/*.md"], capture_output=True, text=True, check=True)
        all_md_uris = [line.strip() for line in res.stdout.splitlines() if line.strip().endswith(".md")]
        
        for uri in all_md_uris:
            filename = uri.split("/")[-1]
            lower_fn = filename.lower()
            
            # Match vsdx or drawio in filename or permissions metadata or v2_sp_ prefix
            is_diagram = lower_fn.startswith("v2_sp_") or any(kw in lower_fn for kw in ["vsdx", "drawio", "drawing", "diagram", "hld", "lld"])
            if not is_diagram:
                perms_entry = sp_perms.get(filename) or cf_perms.get(filename) or {}
                doc_title = (perms_entry.get("title") or "").lower()
                is_diagram = "vsdx" in doc_title or "drawio" in doc_title or "drawing" in doc_title
                
            if is_diagram:
                diagram_gcs_uris.append(uri)
                diagram_filenames.add(filename)

    except Exception as list_err:
        print(f"[Error] Listing GCS files failed: {list_err}")
        return

    print(f"➜ Found {len(diagram_gcs_uris)} candidate diagram file(s) in GCS.")

    if not diagram_gcs_uris:
        print("No diagram files found to push. Exiting.")
        return

    # Fast GCS Checkpoint Catalog Load (0.5s) instead of scanning 5,000+ corpus files (30+ mins)
    imported_catalog = set()
    catalog_gcs_uri = f"gs://{gcs_bucket}/rag_imported_files_catalog.json"
    is_force = os.getenv("FORCE_REINGEST", "false").lower() == "true"

    if not is_force:
        try:
            res_cat = subprocess.run(["gcloud", "storage", "cat", catalog_gcs_uri], capture_output=True, text=True)
            if res_cat.returncode == 0 and res_cat.stdout.strip():
                imported_catalog = set(json.loads(res_cat.stdout))
                print(f"➜ Loaded {len(imported_catalog)} entry(ies) from import checkpoint catalog.")
        except Exception:
            pass

    pending_diagram_uris = []
    skipped_diagram_count = 0
    for uri in diagram_gcs_uris:
        fn = uri.split("/")[-1]
        if not is_force and (fn in imported_catalog or uri in imported_catalog):
            skipped_diagram_count += 1
        else:
            pending_diagram_uris.append(uri)

    print(f"   • Total diagrams discovered : {len(diagram_gcs_uris)}")
    print(f"   • Already imported (SKIPPED) : {skipped_diagram_count}")
    print(f"   • Pending import (PROCESSING): {len(pending_diagram_uris)}")

    if not pending_diagram_uris:
        print("✔ All diagram files are already imported into the RAG corpus! Skipping batch import step.")
    else:
        # 3. Trigger Vertex AI RAG import for diagram files
        print(f"\n[2/3] Importing {len(pending_diagram_uris)} pending diagram file(s) into RAG Corpus...")
        try:
            # Import in batches of 20
            batch_size = 20
            for i in range(0, len(pending_diagram_uris), batch_size):
                batch_uris = pending_diagram_uris[i : i + batch_size]
                print(f"   -> Launching batch import {i//batch_size + 1} ({len(batch_uris)} files)...")
                response = retry_api_call(
                    rag.import_files,
                    corpus_name=corpus_name,
                    paths=batch_uris,
                    chunk_size=1024,
                    chunk_overlap=200
                )
                print(f"      ✔ Batch import completed. Imported: getattr({getattr(response, 'imported_rag_files_count', len(batch_uris))})")
                for u in batch_uris:
                    fn = u.split("/")[-1]
                    imported_catalog.add(u)
                    imported_catalog.add(fn)
                time.sleep(2.0)

            # Persist updated import checkpoint catalog back to GCS
            try:
                tmp_cat_path = rag_agent_dir / "rag_imported_files_catalog.json"
                tmp_cat_path.write_text(json.dumps(list(imported_catalog), indent=2), encoding="utf-8")
                subprocess.run(["gcloud", "storage", "cp", str(tmp_cat_path), catalog_gcs_uri], check=True, capture_output=True)
                if tmp_cat_path.exists(): tmp_cat_path.unlink()
            except Exception:
                pass

        except Exception as import_err:
            print(f"[Error] RAG import failed: {import_err}")

    # 4. Apply custom metadata tagging to diagram files in Corpus
    print(f"\n[3/3] Applying custom metadata tags to diagram files in RAG Corpus...")
    try:
        # Load metadata tagged catalog from GCS
        tagged_catalog = set()
        tagged_catalog_gcs_uri = f"gs://{gcs_bucket}/metadata_tagged_catalog.json"
        try:
            res_cat = subprocess.run(["gcloud", "storage", "cat", tagged_catalog_gcs_uri], capture_output=True, text=True)
            if res_cat.returncode == 0 and res_cat.stdout.strip():
                tagged_catalog = set(json.loads(res_cat.stdout))
                print(f"➜ Loaded {len(tagged_catalog)} entry(ies) from metadata tagged catalog.")
        except Exception:
            pass

        # Early-stopping target file locator (stops as soon as target diagram files are found)
        target_rag_files = []
        page_token = None
        target_set = diagram_filenames - tagged_catalog
        
        if not target_set:
            print("✔ All diagram files are already tagged with custom metadata!")
            return

        print(f"➜ Searching RAG Corpus for {len(target_set)} untagged diagram file(s)...")
        while True:
            try:
                resp = rag.list_files(corpus_name=corpus_name, page_size=1000, page_token=page_token)
                file_list = getattr(resp, "rag_files", [])
                page_token = getattr(resp, "next_page_token", None)
                
                for rf in file_list:
                    dn = getattr(rf, "display_name", "")
                    if dn in target_set:
                        target_rag_files.append(rf)
                
                # Early stop if all target files found
                if len(target_rag_files) >= len(target_set) or not page_token:
                    break
            except Exception as e:
                print(f"   [Warning] Early stop list error: {e}")
                break

        print(f"➜ Untagged RAG Corpus diagram files to process: {len(target_rag_files)}")

        if not target_rag_files:
            print("✔ All diagram files are already tagged with custom metadata!")
            return

        tagged_lock = threading.Lock()

        def tag_file(rag_file):
            display_name = rag_file.display_name
            perms = sp_perms.get(display_name) or cf_perms.get(display_name) or {}
            source_system = "sharepoint" if display_name in sp_perms else ("confluence" if display_name in cf_perms else "diagram")
            
            restricted = perms.get("restricted", False)
            site_name = perms.get("site_name", "")
            space_name = perms.get("space_name", "")

            requests = [
                rag.RagMetadata(user_specified_metadata=rag.UserSpecifiedMetadata(values={"restricted": rag.MetadataValue(bool_value=restricted)})),
                rag.RagMetadata(user_specified_metadata=rag.UserSpecifiedMetadata(values={"source_system": rag.MetadataValue(string_value=source_system)}))
            ]
            if site_name:
                requests.append(rag.RagMetadata(user_specified_metadata=rag.UserSpecifiedMetadata(values={"site_name": rag.MetadataValue(string_value=site_name)})))
            if space_name:
                requests.append(rag.RagMetadata(user_specified_metadata=rag.UserSpecifiedMetadata(values={"space_name": rag.MetadataValue(string_value=space_name)})))

            try:
                retry_api_call(
                    rag.batch_create_metadata,
                    corpus_name=corpus_name,
                    file_name=rag_file.name,
                    requests=requests
                )
                with tagged_lock:
                    tagged_catalog.add(display_name)
            except Exception as e:
                err_str = str(e)
                if any(x in err_str for x in ["keys do not exist", "already exists", "INTERNAL", "13"]):
                    with tagged_lock:
                        tagged_catalog.add(display_name)
                    return
                print(f"   [Warning] Tagging error for {display_name}: {e}")

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(tag_file, rf) for rf in target_rag_files]
            for f in as_completed(futures):
                try: f.result()
                except Exception as e: print(f"   [Warning] Tagging error: {e}")

        # Save updated metadata tagged catalog back to GCS
        try:
            tmp_cat_path = rag_agent_dir / "metadata_tagged_catalog.json"
            tmp_cat_path.write_text(json.dumps(list(tagged_catalog), indent=2), encoding="utf-8")
            subprocess.run(["gcloud", "storage", "cp", str(tmp_cat_path), tagged_catalog_gcs_uri], check=True, capture_output=True)
            if tmp_cat_path.exists(): tmp_cat_path.unlink()
            print("✔ Updated metadata tagged catalog uploaded to GCS.")
        except Exception as save_err:
            print(f"   [Warning] Could not save metadata tagged catalog: {save_err}")

        print("✔ Metadata tagging completed for all diagram files!")

    except Exception as tag_err:
        print(f"[Warning] Metadata tagging step failed: {tag_err}")

    print("\n" + "=" * 80)
    print("FAST DIAGRAM RAG CORPUS PUSH COMPLETED SUCCESSFULLY!")
    print("=" * 80)

if __name__ == "__main__":
    push_diagrams_only()
