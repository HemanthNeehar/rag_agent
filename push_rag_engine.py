import os
import subprocess
from pathlib import Path
from dotenv import load_dotenv
import vertexai
from vertexai.preview import rag

workspace_dir = Path(__file__).parent.parent
load_dotenv(workspace_dir / ".env")


def trigger_confluence_gcs_to_rag_engine():
    """
    Imports files from the staging GCS bucket into the Vertex AI RAG corpus.
    Uses layout-aware chunking so source_url metadata lines embedded at the
    top of each document are preserved in every chunk, enabling the RAG engine
    to return source page links alongside retrieved content.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    corpus_id = os.getenv("RAG_CORPUS_ID", "6301661778598166528")
    gcs_bucket = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc")

    if not project_id:
        raise ValueError("GOOGLE_CLOUD_PROJECT is not set in the environment.")

    gcs_uri = f"gs://{gcs_bucket}/"
    corpus_name = f"projects/{project_id}/locations/{location}/ragCorpora/{corpus_id}"

    print(f"Initializing Vertex AI: project={project_id}, location={location}")
    vertexai.init(project=project_id, location=location)

    # Optional: clean purge of existing RAG corpus files when FORCE_REINGEST is set
    if os.getenv("FORCE_REINGEST", "false").lower() == "true":
        print("  -> FORCE_REINGEST is true. Purging existing files in the RAG corpus for a clean sync...")
        try:
            existing_files = list(rag.list_files(corpus_name=corpus_name))
            if existing_files:
                print(f"     Found {len(existing_files)} existing file(s) in corpus. Deleting...")
                for f_item in existing_files:
                    try:
                        print(f"       Deleting: {f_item.display_name} ({f_item.name})")
                        rag.delete_file(name=f_item.name)
                    except Exception as del_err:
                        print(f"       [Warning] Failed to delete file {f_item.display_name}: {del_err}")
                print("     Clean purge complete.")
            else:
                print("     No existing files found in the corpus.")
        except Exception as list_err:
            print(f"  [Warning] Failed to list or purge existing files: {list_err}")

    # Enumerate only the .md files at the bucket root (not the images/ subdirectory).
    # The RAG engine supports text formats only (.md, .txt, .pdf).
    # Binary image files under images/ will always fail — exclude them by listing
    # individual .md URIs instead of passing the whole bucket URI.
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
            # Skip anything under images/ (binary files, even if named .png.md)
            if obj_name.startswith("images/"):
                continue
            # Skip non-markdown files — RAG engine only processes text formats
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
        print("  [Error] No .md files found in bucket root. Did ingest_gcs.py run successfully?")
        return

    print(f"  Found {len(md_uris)} .md file(s) to import.")
    print(f"Target corpus    : {corpus_name}")

    # Ingest files in batches of 20 to respect the 25 GCS URIs per call API limit
    batch_size = 20
    imported_count = 0
    failed_count = 0
    
    print(f"  -> Launching import in batches of {batch_size} to respect Vertex RAG API limits...")
    for i in range(0, len(md_uris), batch_size):
        batch = md_uris[i:i + batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (len(md_uris) + batch_size - 1) // batch_size
        print(f"     -> Importing batch {batch_num}/{total_batches} ({len(batch)} files)...")
        try:
            response = rag.import_files(
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

    print(f"\nIngestion complete.")
    print(f"  Imported : {imported_count} file(s)")
    print(f"  Failed   : {failed_count} file(s)")
    if failed_count:
        print("  [Warning] Some files failed. Run the following to inspect:")
        print(f"  gcloud ai rag files list --corpus={corpus_name} --location={location}")

    # Programmatically apply metadata tags (restricted, source_system, space_name, site_name)
    apply_metadata_to_corpus_files()


def apply_metadata_to_corpus_files():
    """
    Loads permissions maps from GCS and programmatically tags imported RAG files
    with custom metadata fields (restricted, source_system, space_name, site_name)
    using vertexai.preview.rag.batch_create_metadata.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    corpus_id = os.getenv("RAG_CORPUS_ID", "6301661778598166528")
    gcs_bucket = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc")
    corpus_name = f"projects/{project_id}/locations/{location}/ragCorpora/{corpus_id}"

    import json
    import subprocess
    
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

    # 2. Iterate through files in RAG Corpus and apply metadata
    print("\nScanning RAG Corpus files to apply custom metadata...")
    try:
        corpus_files = list(rag.list_files(corpus_name=corpus_name))
        print(f"  Found {len(corpus_files)} file(s) in RAG corpus.")
        
        for rag_file in corpus_files:
            display_name = rag_file.display_name
            metadata_entry = None
            source_system = None
            
            if display_name in confluence_map:
                metadata_entry = confluence_map[display_name]
                source_system = "confluence"
            elif display_name in sharepoint_map:
                metadata_entry = sharepoint_map[display_name]
                source_system = "sharepoint"
                
            if not metadata_entry:
                print(f"  -> No permission map entry for {display_name}. Skipping metadata tagging.")
                continue
                
            restricted = metadata_entry.get("restricted", False)
            space_name = metadata_entry.get("space_name", "")
            site_name = metadata_entry.get("site_name", "")
            
            print(f"  -> Tagging {display_name} ({source_system}): restricted={restricted}, space_name={space_name}, site_name={site_name}")
            
            try:
                # Build MetadataValues
                values = {
                    "restricted": rag.MetadataValue(bool_value=restricted),
                    "source_system": rag.MetadataValue(string_value=source_system)
                }
                if space_name:
                    values["space_name"] = rag.MetadataValue(string_value=space_name)
                if site_name:
                    values["site_name"] = rag.MetadataValue(string_value=site_name)
                    
                user_metadata = rag.UserSpecifiedMetadata(values=values)
                rag_metadata = rag.RagMetadata(user_specified_metadata=user_metadata)
                
                rag.batch_create_metadata(
                    corpus_name=corpus_name,
                    file_name=rag_file.name,
                    requests=[rag_metadata]
                )
                print(f"     Successfully tagged.")
            except Exception as tag_err:
                print(f"     [Error] Failed tagging file {display_name}: {tag_err}")
                
    except Exception as list_err:
        print(f"  [Error] Failed listing corpus files: {list_err}")


if __name__ == "__main__":
    trigger_confluence_gcs_to_rag_engine()

