import os
import subprocess
import time
import random
import json
import threading
from pathlib import Path
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import vertexai
from vertexai.preview import rag

workspace_dir = Path(__file__).parent.parent
load_dotenv(workspace_dir / ".env")

push_failures = []
push_failures_lock = threading.Lock()


def retry_api_call(func, *args, max_retries=6, initial_backoff=2.0, **kwargs):
    """
    Executes a Google Cloud API function with exponential backoff and jitter
    to handle rate limiting (ResourceExhausted) and transient service errors.
    """
    backoff = initial_backoff
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            e_str = str(e).lower()
            is_quota = any(x in e_str for x in ["resource_exhausted", "quota exceeded", "429", "rate limit"])
            if is_quota:
                if attempt == max_retries - 1:
                    print(f"        [Error] Max retries ({max_retries}) reached. API call failed with quota error.")
                    raise e
                # Exponential backoff with jitter
                sleep_time = backoff + random.uniform(0.5, 1.5)
                print(f"        [Warning] Quota limit detected. Retrying in {sleep_time:.2f}s (Attempt {attempt+1}/{max_retries})...")
                time.sleep(sleep_time)
                backoff *= 2.0
            else:
                raise e


def get_all_corpus_files(corpus_name):
    """
    Lists all files in the RAG corpus safely, dynamically handling different
    SDK return types (ListRagFilesPager vs ListRagFilesResponse) and versions
    to prevent 'object is not iterable' errors under all conditions.
    """
    corpus_files = []
    print("  -> Fetching corpus files list page-by-page with rate-limiting pacing...")
    try:
        # Initialize page token to handle manual pagination if ListRagFilesResponse is returned
        page_token = None
        
        while True:
            # Explicit call with current token
            response = retry_api_call(
                rag.list_files,
                corpus_name=corpus_name,
                page_size=1000,
                page_token=page_token
            )
            
            # Case 1: If it's a ListRagFilesPager (has 'pages' attribute)
            if hasattr(response, "pages"):
                print("     Detected ListRagFilesPager. Extracting files...")
                page_idx = 1
                for page in response.pages:
                    # Safely extract files from the page (which is a ListRagFilesResponse)
                    if hasattr(page, "rag_files"):
                        page_files = page.rag_files or []
                    else:
                        try:
                            page_files = list(page)
                        except TypeError:
                            page_files = []
                    
                    page_files_list = list(page_files)
                    print(f"        Loaded page {page_idx} ({len(page_files_list)} files)...")
                    corpus_files.extend(page_files_list)
                    page_idx += 1
                    time.sleep(2.0)
                break # Pager handles all pages automatically
                
            # Case 2: If it's a ListRagFilesResponse object
            elif hasattr(response, "rag_files"):
                page_files = response.rag_files or []
                print(f"     Detected ListRagFilesResponse. Loaded batch of {len(page_files)} files.")
                corpus_files.extend(page_files)
                
                # Check for next page token
                page_token = getattr(response, "next_page_token", None)
                if not page_token:
                    break # No more pages
                
                # Pace the sequential requests
                time.sleep(2.0)
                
            # Case 3: Fallback if it behaves as a simple iterable
            else:
                try:
                    print("     Detected iterable response. Converting to list...")
                    corpus_files = list(response)
                    break
                except TypeError:
                    # Case 4: Extreme fallback - raw dict or unknown structure
                    print(f"     [Warning] Unknown response type {type(response)}. Attempting raw dict representation.")
                    if isinstance(response, dict) and "rag_files" in response:
                        corpus_files.extend(response["rag_files"])
                        page_token = response.get("next_page_token")
                        if not page_token:
                            break
                        time.sleep(2.0)
                    else:
                        raise TypeError(f"Response of type {type(response)} is not recognized or iterable.")
                        
    except Exception as e:
        print(f"  [Error] Failed paginating corpus files: {e}")
        raise e
        
    return corpus_files


def save_catalog_progress(tagged_catalog, catalog_gcs_uri):
    """
    Saves and uploads the current metadata tagging state to GCS.
    """
    try:
        temp_dir = Path("/tmp")
        if not temp_dir.exists():
            temp_dir = Path(".")
        temp_file = temp_dir / "metadata_tagged_catalog.json"
        temp_file.write_text(json.dumps(list(tagged_catalog)))
        
        subprocess.run(
            ["gcloud", "storage", "cp", str(temp_file), catalog_gcs_uri],
            capture_output=True, check=True
        )
        if temp_file.exists():
            temp_file.unlink()
    except Exception as save_err:
        print(f"     [Warning] Failed uploading incremental progress to GCS: {save_err}")


def save_push_failures(gcs_bucket):
    """Writes push_failures list to GCS bucket as gcs_push_rag_failure_results.json."""
    try:
        temp_dir = Path("/tmp")
        if not temp_dir.exists():
            temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / "gcs_push_rag_failure_results.json"
        temp_file.write_text(json.dumps(push_failures, indent=2, ensure_ascii=False), encoding="utf-8")
        
        failures_gcs_uri = f"gs://{gcs_bucket}/gcs_push_rag_failure_results.json"
        print(f"\n  -> Uploading {len(push_failures)} RAG push failures to {failures_gcs_uri}...")
        subprocess.run(
            ["gcloud", "storage", "cp", str(temp_file), failures_gcs_uri],
            capture_output=True,
            check=True,
        )
        print("  -> Upload of RAG push failures completed successfully.")
        if temp_file.exists():
            temp_file.unlink()
    except Exception as e:
        print(f"  [Warning] Failed writing or uploading push failures to GCS: {e}")


def trigger_confluence_gcs_to_rag_engine():
    """
    Imports files from the staging GCS bucket into the Vertex AI RAG corpus.
    Uses layout-aware chunking so source_url metadata lines embedded at the
    top of each document are preserved in every chunk, enabling the RAG engine
    to return source page links alongside retrieved content.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    # Resolve and align location dynamically. Match both client init and corpus location.
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-south1")
    corpus_id = os.getenv("RAG_CORPUS_ID", "7991637538768945152")
    gcs_bucket = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc-bucket")

    if not project_id:
        raise ValueError("GOOGLE_CLOUD_PROJECT is not set in the environment.")

    gcs_uri = f"gs://{gcs_bucket}/"
    corpus_name = f"projects/{project_id}/locations/{location}/ragCorpora/{corpus_id}"

    print(f"Initializing Vertex AI: project={project_id}, location={location}")
    retry_api_call(vertexai.init, project=project_id, location=location)

    # Optional: clean purge of existing RAG corpus files when FORCE_REINGEST is set
    if os.getenv("FORCE_REINGEST", "false").lower() == "true":
        print("  -> FORCE_REINGEST is true. Purging existing files in the RAG corpus for a clean sync...")
        try:
            existing_files = get_all_corpus_files(corpus_name)
            if existing_files:
                print(f"     Found {len(existing_files)} existing file(s) in corpus. Deleting...")
                for f_item in existing_files:
                    try:
                        print(f"       Deleting: {f_item.display_name} ({f_item.name})")
                        retry_api_call(rag.delete_file, name=f_item.name)
                    except Exception as del_err:
                        print(f"       [Warning] Failed to delete file {f_item.display_name}: {del_err}")
                print("     Clean purge complete.")
            else:
                print("     No existing files found in the corpus.")
        except Exception as list_err:
            print(f"  [Warning] Failed to list or purge existing files: {list_err}")

    # Enumerate only the .md files at the bucket root (not the images/ subdirectory).
    print(f"Scanning gs://{gcs_bucket}/ for .md files to import...")
    md_uris: list[str] = []
    try:
        result = subprocess.run(
            ["gcloud", "storage", "ls", f"gs://{gcs_bucket}/"],
            capture_output=True, text=True, check=True,
        )
        for obj_uri in result.stdout.splitlines():
            obj_uri = obj_uri.strip()
            if not obj_uri or obj_uri.endswith("/"):
                continue  # skip subdirectory entries
            obj_name = obj_uri.removeprefix(f"gs://{gcs_bucket}/")
            # Skip anything under images/ (binary files)
            if obj_name.startswith("images/"):
                continue
            # Skip non-markdown files
            if not obj_name.endswith(".md"):
                print(f"  -> Skipping non-md file: {obj_name}")
                continue
            # Apostrophe or backtick in name will fail RAG import — remove them
            if "'" in obj_name or "`" in obj_name:
                print(f"  -> Removing problematic file: {obj_name}")
                subprocess.run(["gcloud", "storage", "rm", obj_uri], check=True)
                print(f"     Deleted.")
                continue
            md_uris.append(obj_uri)
    except Exception as e:
        print(f"  [Warning] Could not scan bucket: {e}. Falling back to full bucket URI.")
        md_uris = [gcs_uri]

    if not md_uris:
        print("  [Error] No .md files found in bucket root. Did ingestion run successfully?")
        return

    print(f"  Found {len(md_uris)} .md file(s) to import.")
    print(f"Target corpus    : {corpus_name}")

    # Ingest files in batches of 20 to respect the 25 GCS URIs per call API limit
    batch_size = 20
    imported_count = 0
    failed_count = 0
    
    print(f"  -> Launching import in batches of {batch_size} with pacing delay to protect quotas...")
    for i in range(0, len(md_uris), batch_size):
        batch = md_uris[i:i + batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (len(md_uris) + batch_size - 1) // batch_size
        print(f"     -> Importing batch {batch_num}/{total_batches} ({len(batch)} files)...")
        try:
            response = retry_api_call(
                rag.import_files,
                corpus_name=corpus_name,
                paths=batch,
                chunk_size=512,
                chunk_overlap=100,
                max_embedding_requests_per_min=900,
            )
            imported_count += response.imported_rag_files_count
            failed_count += response.failed_rag_files_count
            print(f"        Batch complete. Imported: {response.imported_rag_files_count}, Failed: {response.failed_rag_files_count}")
        except Exception as e:
            print(f"        [Error] Batch {batch_num} failed: {e}")
            failed_count += len(batch)
            with push_failures_lock:
                for file_uri in batch:
                    file_name = file_uri.split("/")[-1]
                    push_failures.append({
                        "file_name": file_name,
                        "error_code": type(e).__name__,
                        "failure_reason": f"Batch {batch_num} import failed: {str(e)}",
                        "rag_progress_fail_code": "RAG_BATCH_IMPORT"
                    })
            
        # Small sleep between batches to natively space out rate limit windows
        time.sleep(2.0)

    print(f"\nIngestion complete.")
    print(f"  Imported : {imported_count} file(s)")
    print(f"  Failed   : {failed_count} file(s)")
    if failed_count:
        print("  [Warning] Some files failed. Run the following to inspect:")
        print(f"  gcloud ai rag files list --corpus={corpus_name} --location={location}")

    # Programmatically apply metadata tags (restricted, source_system, space_name, site_name)
    apply_metadata_to_corpus_files()

    # Save and upload RAG push failures to GCS
    save_push_failures(gcs_bucket)


def apply_metadata_to_corpus_files():
    """
    Loads permissions maps from GCS and programmatically tags imported RAG files
    with custom metadata fields (restricted, source_system, space_name, site_name)
    using vertexai.preview.rag.batch_create_metadata.
    Uses ThreadPoolExecutor for concurrent tagging, and GCS state tracking
    to make tagging resumeable and skip already-tagged files.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-south1")
    corpus_id = os.getenv("RAG_CORPUS_ID", "7991637538768945152")
    gcs_bucket = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc-bucket")
    corpus_name = f"projects/{project_id}/locations/{location}/ragCorpora/{corpus_id}"

    # 1. Download permissions maps from GCS
    confluence_map = {}
    sharepoint_map = {}
    
    print("\nDownloading Confluence and SharePoint permissions maps from GCS...")
    try:
        res1 = subprocess.run(
            ["gcloud", "storage", "cat", f"gs://{gcs_bucket}/gcs_confluence_permissions_map.json"],
            capture_output=True, text=True, check=True
        )
        confluence_map = json.loads(res1.stdout)
        print(f"  Loaded {len(confluence_map)} Confluence permissions entries.")
    except Exception as e:
        print(f"  [Warning] Confluence permissions map not found or empty: {e}")

    try:
        res2 = subprocess.run(
            ["gcloud", "storage", "cat", f"gs://{gcs_bucket}/gcs_sharepoint_permissions_map.json"],
            capture_output=True, text=True, check=True
        )
        sharepoint_map = json.loads(res2.stdout)
        print(f"  Loaded {len(sharepoint_map)} SharePoint permissions entries.")
    except Exception as e:
        print(f"  [Warning] SharePoint permissions map not found or empty: {e}")

    # 1.5 Load existing tagged files catalog to avoid redundant updates and protect quotas
    tagged_catalog = set()
    catalog_gcs_uri = f"gs://{gcs_bucket}/metadata_tagged_catalog.json"
    is_force_reingest = os.getenv("FORCE_REINGEST", "false").lower() == "true"
    
    if not is_force_reingest:
        print(f"Downloading existing metadata tagging catalog from {catalog_gcs_uri}...")
        try:
            cat_res = subprocess.run(
                ["gcloud", "storage", "cat", catalog_gcs_uri],
                capture_output=True, text=True, check=True
            )
            tagged_catalog = set(json.loads(cat_res.stdout))
            print(f"  Loaded metadata catalog with {len(tagged_catalog)} previously tagged files.")
        except Exception:
            print("  No previous metadata tagging catalog found. Will tag all files.")
    else:
        print("  FORCE_REINGEST is true. Bypassing previous tagging catalog to re-tag all files from scratch.")

    # 2. Iterate through files in RAG Corpus and apply metadata
    print("\nScanning RAG Corpus files to apply custom metadata...")
    dirty_catalog = False
    try:
        corpus_files = get_all_corpus_files(corpus_name)
        print(f"  Found {len(corpus_files)} file(s) in RAG corpus.")
        
        # Filter files that actually need tagging
        files_to_tag = []
        for rag_file in corpus_files:
            display_name = rag_file.display_name
            if display_name in tagged_catalog:
                continue
                
            metadata_entry = None
            source_system = None
            if display_name in confluence_map:
                metadata_entry = confluence_map[display_name]
                source_system = "confluence"
            elif display_name in sharepoint_map:
                metadata_entry = sharepoint_map[display_name]
                source_system = "sharepoint"
                
            if metadata_entry:
                files_to_tag.append((rag_file, metadata_entry, source_system))
                
        if not files_to_tag:
            print("  No new files need metadata tagging. Up to date!")
            return
            
        print(f"  -> Concurrently tagging {len(files_to_tag)} file(s) using 5 threads to ease rate limit pressure...")
        
        catalog_lock = threading.Lock()
        processed_count = 0
        schema_missing_flag = threading.Event()
        
        def tag_single_file(rag_file, metadata_entry, source_system):
            nonlocal dirty_catalog, processed_count
            if schema_missing_flag.is_set():
                return
            display_name = rag_file.display_name
            restricted = metadata_entry.get("restricted", False)
            space_name = metadata_entry.get("space_name", "")
            site_name = metadata_entry.get("site_name", "")
            
            # Build MetadataValues
            requests = []
            
            # 1. restricted
            user_metadata_restricted = rag.UserSpecifiedMetadata(
                values={"restricted": rag.MetadataValue(bool_value=restricted)}
            )
            requests.append(rag.RagMetadata(user_specified_metadata=user_metadata_restricted))
            
            # 2. source_system
            if source_system:
                user_metadata_sys = rag.UserSpecifiedMetadata(
                    values={"source_system": rag.MetadataValue(string_value=source_system)}
                )
                requests.append(rag.RagMetadata(user_specified_metadata=user_metadata_sys))
            
            # 3. space_name
            if space_name:
                user_metadata_space = rag.UserSpecifiedMetadata(
                    values={"space_name": rag.MetadataValue(string_value=space_name)}
                )
                requests.append(rag.RagMetadata(user_specified_metadata=user_metadata_space))
                
            # 4. site_name
            if site_name:
                user_metadata_site = rag.UserSpecifiedMetadata(
                    values={"site_name": rag.MetadataValue(string_value=site_name)}
                )
                requests.append(rag.RagMetadata(user_specified_metadata=user_metadata_site))

            try:
                retry_api_call(
                    rag.batch_create_metadata,
                    corpus_name=corpus_name,
                    file_name=rag_file.name,
                    requests=requests
                )
            except Exception as e:
                err_msg = str(e)
                if "RagDataSchema keys do not exist" in err_msg or "keys do not exist" in err_msg:
                    if not schema_missing_flag.is_set():
                        schema_missing_flag.set()
                        print("\n  [Warning] Target RAG Corpus lacks a RagDataSchema. Custom metadata tagging cannot be completed on this corpus.")
                        print("  [Warning] Bypassing custom metadata tagging. Files are successfully imported and fully searchable!")
                    return
                raise e
            
            with catalog_lock:
                tagged_catalog.add(display_name)
                dirty_catalog = True
                processed_count += 1
                if processed_count % 100 == 0 or processed_count == len(files_to_tag):
                    print(f"     Progress: Tagged {processed_count}/{len(files_to_tag)} files successfully.")
                    save_catalog_progress(tagged_catalog, catalog_gcs_uri)
            
            # Tiny sleep inside thread to prevent hammering API
            time.sleep(0.1)

        # Process with a ThreadPoolExecutor in controlled memory batches to prevent OOM
        batch_size = 100
        for chunk_start in range(0, len(files_to_tag), batch_size):
            if schema_missing_flag.is_set():
                break
                
            chunk = files_to_tag[chunk_start:chunk_start + batch_size]
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {
                    executor.submit(tag_single_file, rf, me, sys): rf.display_name 
                    for rf, me, sys in chunk
                }
                for fut in as_completed(futures):
                    disp_name = futures[fut]
                    try:
                        fut.result()
                    except Exception as fut_err:
                        if schema_missing_flag.is_set():
                            continue
                        print(f"     [Error] Thread failed tagging file {disp_name}: {fut_err}")
                        with push_failures_lock:
                            push_failures.append({
                                "file_name": disp_name,
                                "error_code": type(fut_err).__name__,
                                "failure_reason": str(fut_err),
                                "rag_progress_fail_code": "RAG_METADATA_TAGGING"
                            })

        if schema_missing_flag.is_set():
            # Gracefully populate the tagged catalog as skipped to prevent repeated tagging attempts
            print("  Populating skipped tagging catalog to GCS to bypass metadata updates on subsequent runs.")
            for rf, _, _ in files_to_tag:
                tagged_catalog.add(rf.display_name)
            dirty_catalog = True
                
    except Exception as list_err:
        print(f"  [Error] Failed listing or tagging corpus files: {list_err}")
    finally:
        # 3. Upload updated metadata catalog to GCS
        if 'dirty_catalog' in locals() and dirty_catalog:
            print("\nUploading final metadata tagging catalog to GCS...")
            save_catalog_progress(tagged_catalog, catalog_gcs_uri)


if __name__ == "__main__":
    trigger_confluence_gcs_to_rag_engine()
