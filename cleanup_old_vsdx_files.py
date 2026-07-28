#!/usr/bin/env python3
"""
Legacy/Duplicate Visio (.vsdx.md) RAG File Cleanup Utility

Finds and safely deletes old, poorly formatted Visio files (e.g. *.vsdx.md or duplicate legacy Visio imports)
from the Vertex AI RAG Corpus.
"""

import os
import sys
import argparse
from pathlib import Path
from dotenv import load_dotenv

rag_agent_dir = Path(__file__).parent.resolve()
load_dotenv(rag_agent_dir / ".env", override=True)

import vertexai
from vertexai.preview import rag

def init_vertex():
    project_id = os.getenv("RAG_PROJECT_ID", "405625028294")
    location = os.getenv("RAG_LOCATION", "us-central1")
    corpus_id = os.getenv("RAG_CORPUS_ID", "6301662991053422592")
    corpus_name = f"projects/{project_id}/locations/{location}/ragCorpora/{corpus_id}"
    
    vertexai.init(project=project_id, location=location)
    return corpus_name, project_id, location, corpus_id

def cleanup_legacy_vsdx_files(execute_delete=False, target_pattern="vsdx.md"):
    corpus_name, project_id, location, corpus_id = init_vertex()
    
    print("=" * 80)
    print("STARTING LEGACY VISIO FILE CLEANUP IN RAG CORPUS")
    print(f"Target Corpus : {corpus_name}")
    print(f"Search Pattern: '{target_pattern}'")
    print(f"Execution Mode: {'DESTROY/DELETE' if execute_delete else 'DRY RUN (Preview Only)'}")
    print("=" * 80)

    page_token = None
    scanned_total = 0
    matched_files = []

    print("\nScanning RAG Corpus files...")
    while True:
        try:
            response = rag.list_files(
                corpus_name=corpus_name,
                page_size=1000,
                page_token=page_token
            )
            file_list = getattr(response, "rag_files", [])
            page_token = getattr(response, "next_page_token", None)
            
            for rf in file_list:
                scanned_total += 1
                display_name = getattr(rf, "display_name", "")
                
                # Check if file matches target legacy pattern (e.g., ends with .vsdx.md or contains vsdx.md)
                if target_pattern.lower() in display_name.lower():
                    matched_files.append(rf)
                    print(f"  [{len(matched_files)}] Target legacy file found: {display_name}")
                    print(f"      Resource ID: {rf.name}")

            if not page_token:
                break
        except Exception as e:
            print(f"  [Error] Listing failed: {e}")
            break

    print("\n" + "=" * 80)
    print(f"SCAN SUMMARY: Scanned {scanned_total} total file(s) | Found {len(matched_files)} legacy target(s).")
    print("=" * 80)

    if not matched_files:
        print("✔ No legacy or poorly-formatted target files found to delete!")
        return

    if not execute_delete:
        print("\n[DRY RUN PREVIEW] To delete these files, run with --delete:")
        print("  python3 cleanup_old_vsdx_files.py --delete")
        return

    print(f"\n➜ DELETING {len(matched_files)} legacy file(s) from RAG Corpus...")
    deleted_count = 0
    failed_count = 0

    for rf in matched_files:
        try:
            display_name = getattr(rf, "display_name", rf.name)
            print(f"   Deleting ({deleted_count + 1}/{len(matched_files)}): {display_name}...")
            rag.delete_file(name=rf.name)
            deleted_count += 1
            print(f"      ✔ Deleted.")
        except Exception as del_err:
            print(f"      [Error] Could not delete {rf.name}: {del_err}")
            failed_count += 1

    print("\n" + "=" * 80)
    print(f"✔ CLEANUP COMPLETE: Successfully deleted {deleted_count} file(s) ({failed_count} failed).")
    print("=" * 80)

def main():
    parser = argparse.ArgumentParser(description="Cleanup legacy or poorly formatted Visio files from RAG Corpus")
    parser.add_argument("--delete", action="store_true", help="Execute deletion (default is dry-run preview)")
    parser.add_argument("--pattern", type=str, default="vsdx.md", help="Filename pattern to match (default 'vsdx.md')")
    
    args = parser.parse_args()
    cleanup_legacy_vsdx_files(execute_delete=args.delete, target_pattern=args.pattern)

if __name__ == "__main__":
    main()
