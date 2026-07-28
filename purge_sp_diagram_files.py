#!/usr/bin/env python3
"""
Utility script to purge all older 'sp_*' diagram files from both GCS and Vertex AI RAG Corpus.

This allows re-running the enhanced Visio/Draw.io ingestion job (using Gemini 2.5 Pro Multimodal Vision)
from a completely clean state.
"""

import os
import time
import subprocess
from pathlib import Path
from dotenv import load_dotenv

rag_agent_dir = Path(__file__).parent.resolve()
load_dotenv(rag_agent_dir / ".env", override=True)

def purge_sp_files_from_gcs_and_corpus():
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("RAG_PROJECT_ID", "agent-ops-494011")
    corpus_location = os.getenv("RAG_CORPUS_LOCATION", "us-south1")
    corpus_id = os.getenv("RAG_CORPUS_ID", "7991637538768945152")
    gcs_bucket = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc-bucket")

    print("=" * 80)
    print("PURGING OLD 'sp_*' DIAGRAM FILES FROM GCS & VERTEX AI RAG CORPUS")
    print("=" * 80)
    print(f"Project   : {project_id}")
    print(f"Corpus ID : {corpus_id}")
    print(f"GCS Bucket: gs://{gcs_bucket}\n")

    # Step 1: Delete sp_* files from GCS
    print("[1/2] Cleaning up 'sp_*' diagram files from GCS bucket...")
    gcs_cmd = ["gcloud", "storage", "rm", f"gs://{gcs_bucket}/sp_*"]
    try:
        res = subprocess.run(gcs_cmd, capture_output=True, text=True)
        if res.returncode == 0:
            print("  ✔ Successfully deleted 'sp_*' files from GCS.")
        else:
            print(f"  ℹ GCS removal output: {res.stderr.strip() or res.stdout.strip()}")
    except Exception as e:
        print(f"  ⚠️ Warning during GCS cleanup: {e}")

    # Step 2: Delete sp_* files from Vertex AI RAG Corpus
    print("\n[2/2] Scanning Vertex AI RAG Corpus for 'sp_*' diagram files...")
    try:
        import vertexai
        from vertexai.preview import rag

        vertexai.init(project=project_id, location=corpus_location)
        corpus_name = f"projects/{project_id}/locations/{corpus_location}/ragCorpora/{corpus_id}"

        print("Listing files in Corpus...")
        page_token = None
        sp_corpus_files = []

        while True:
            response = rag.list_files(
                corpus_name=corpus_name,
                page_size=1000,
                page_token=page_token
            )
            
            files = []
            if hasattr(response, "pages"):
                for page in response.pages:
                    files.extend(list(getattr(page, "rag_files", page) or []))
                break
            elif hasattr(response, "rag_files"):
                files = response.rag_files or []
                page_token = getattr(response, "next_page_token", None)
            
            for f in files:
                display_name = getattr(f, "display_name", "") or getattr(f, "name", "")
                if display_name.startswith("sp_") or "sp_" in display_name:
                    sp_corpus_files.append((display_name, f.name))

            if not page_token:
                break
            time.sleep(1.0)

        print(f"  Found {len(sp_corpus_files)} 'sp_*' file(s) in Corpus.")

        if sp_corpus_files:
            deleted = 0
            errors = 0
            for idx, (dname, rname) in enumerate(sp_corpus_files, 1):
                print(f"  [{idx}/{len(sp_corpus_files)}] Deleting '{dname}' ({rname})...", end="", flush=True)
                try:
                    rag.delete_file(name=rname)
                    print(" ✔ Deleted")
                    deleted += 1
                except Exception as del_err:
                    print(f" ❌ Error: {del_err}")
                    errors += 1
                time.sleep(0.5)

            print(f"\n✔ Corpus Purge Complete: {deleted} deleted, {errors} errors.")
        else:
            print("✔ No 'sp_*' files found in RAG Corpus.")

    except Exception as e:
        print(f"❌ Error during RAG Corpus scan/cleanup: {e}")

    # Remove local checkpoint catalog if exists
    catalog_path = rag_agent_dir / "rag_imported_files_catalog.json"
    if catalog_path.exists():
        try:
            catalog_path.unlink()
            print("\n✔ Reset local import catalog checkpoint.")
        except Exception:
            pass

    print("\n" + "=" * 80)
    print("PURGE COMPLETE: Ready to re-run enhanced Gemini Pro Visio Ingestion!")
    print("=" * 80)

if __name__ == "__main__":
    purge_sp_files_from_gcs_and_corpus()
