#!/usr/bin/env python3
"""
Utility script to safely delete old redundant Visio files identified in inspect_corpus_output.md
from the Vertex AI RAG Corpus.

Safety Rule:
- ONLY deletes files whose display name matches old un-prefixed patterns (e.g., 'BMUI_HLD.vsdx.md').
- NEVER deletes newly ingested Visio files starting with 'sp_' (e.g., 'sp_TOM_Application_Site_...').
"""

import os
import re
import time
from pathlib import Path

def purge_old_corpus_files(inspect_file_path: str):
    inspect_path = Path(inspect_file_path)
    if not inspect_path.exists():
        print(f"❌ Error: {inspect_file_path} not found.")
        return

    content = inspect_path.read_text()
    
    # Extract matches: "Matched: <filename>" followed by "Resource ID: <rag_file_name>"
    pattern = re.compile(r"Matched:\s*(?P<filename>[^\n]+)\s*\n\s*Resource ID:\s*(?P<resource_id>[^\n]+)")
    matches = pattern.findall(content)

    if not matches:
        print(f"⚠️ No matching Resource IDs found in {inspect_file_path}.")
        return

    print(f"Found {len(matches)} candidate file(s) in {inspect_file_path} for deletion review:")
    
    to_delete = []
    to_skip = []

    for filename, resource_id in matches:
        filename = filename.strip()
        resource_id = resource_id.strip()

        # Safety Check: Skip newly ingested files with 'sp_' prefix
        if filename.startswith("sp_"):
            to_skip.append((filename, resource_id, "Starts with 'sp_' (New Ingestion)"))
        else:
            to_delete.append((filename, resource_id))

    print(f"\nSummary:")
    print(f"  • Files queued for DELETION : {len(to_delete)}")
    print(f"  • Files PRESERVED (Skipped) : {len(to_skip)}")

    if to_skip:
        print("\nPreserved Files:")
        for fname, rid, reason in to_skip:
            print(f"  - [KEEP] {fname} ({reason})")

    if not to_delete:
        print("\n✔ No old files to delete.")
        return

    print("\nFiles to be DELETED:")
    for idx, (fname, rid) in enumerate(to_delete, 1):
        print(f"  [{idx}] {fname}")
        print(f"      ID: {rid}")

    # Proceed with deletion via Vertex AI RAG SDK
    try:
        import vertexai
        from vertexai.preview import rag

        project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "agent-ops-494011")
        rag_location = os.getenv("RAG_CORPUS_LOCATION", "us-south1")
        vertexai.init(project=project_id, location=rag_location)

        print(f"\nDeleting {len(to_delete)} old file(s) from Corpus...")
        deleted_count = 0
        error_count = 0

        for idx, (fname, rid) in enumerate(to_delete, 1):
            print(f"  [{idx}/{len(to_delete)}] Deleting '{fname}' ({rid})...", end="", flush=True)
            try:
                rag.delete_file(name=rid)
                print(" ✔ Deleted")
                deleted_count += 1
            except Exception as e:
                print(f" ❌ Error: {e}")
                error_count += 1
            time.sleep(0.5)

        print(f"\n==================================================")
        print(f"✔ Purge Complete: {deleted_count} deleted, {error_count} failed, {len(to_skip)} preserved.")
        print(f"==================================================")

    except Exception as e:
        print(f"\n❌ Error initializing Vertex AI RAG client: {e}")

if __name__ == "__main__":
    inspect_file = os.path.join(os.path.dirname(__file__), "inspect_corpus_output.md")
    purge_old_corpus_files(inspect_file)
