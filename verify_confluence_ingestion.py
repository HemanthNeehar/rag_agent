import os
import json
import subprocess
from pathlib import Path
from dotenv import load_dotenv

def main():
    print("=" * 60)
    print("GCS CONFLUENCE INGESTION VERIFIER")
    print("=" * 60)
    
    # Load environment variables
    workspace_dir = Path(__file__).parent.parent
    load_dotenv(workspace_dir / ".env")
    
    bucket_name = os.getenv("RAG_GCS_BUCKET_NAME")
    if not bucket_name:
        print("[Error] RAG_GCS_BUCKET_NAME is not set in environment.")
        return
        
    print(f"Target Bucket: gs://{bucket_name}\n")
    
    # Paths for map/catalog states
    map_path = Path(__file__).parent / "gcs_confluence_map.json"
    sharepoint_map_path = Path(__file__).parent / "gcs_sharepoint_map.json"
    
    # Always attempt to download the latest Confluence map state from GCS
    print(f"-> Downloading latest '{map_path.name}' from GCS bucket...")
    try:
        cmd = f'gcloud storage cp "gs://{bucket_name}/gcs_confluence_map.json" "{map_path}"'
        subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[Warning] Failed to download gcs_confluence_map.json from GCS: {e}. Falling back to local map.")

    # Always attempt to download the latest SharePoint map state from GCS for cross-filtering
    print(f"-> Downloading latest '{sharepoint_map_path.name}' from GCS bucket for cross-filtering...")
    try:
        cmd = f'gcloud storage cp "gs://{bucket_name}/gcs_sharepoint_map.json" "{sharepoint_map_path}"'
        subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[Warning] Failed to download gcs_sharepoint_map.json from GCS for cross-filtering: {e}.")

    if not map_path.exists():
        print("[Error] No map state found locally or on GCS. Please run ingestion first.")
        return
        
    with open(map_path, "r", encoding="utf-8") as f:
        gcs_map = json.load(f)
        
    print(f"-> Found {len(gcs_map)} file(s) registered in 'gcs_confluence_map.json'.")
    
    # Load SharePoint registered files to cross-filter out of Confluence listing
    sharepoint_keys = set()
    if sharepoint_map_path.exists():
        try:
            with open(sharepoint_map_path, "r", encoding="utf-8") as f:
                sharepoint_keys = set(json.load(f).keys())
            print(f"-> Loaded {len(sharepoint_keys)} SharePoint registered file(s) to exclude from Confluence results.")
        except Exception as e:
            print(f"[Warning] Failed loading gcs_sharepoint_map.json: {e}")

    # Query GCS to list all .md files
    print("-> Querying GCS bucket for .md files...")
    try:
        # Run gcloud storage ls to list md files
        cmd = f'gcloud storage ls "gs://{bucket_name}/*.md"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            gcs_files = []
        else:
            gcs_files = [os.path.basename(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    except Exception as e:
        print(f"[Error] Failed to query GCS bucket: {e}")
        return
        
    print(f"-> Found {len(gcs_files)} '.md' file(s) physically present in gs://{bucket_name}/")
    
    # Perform comparison by cross-filtering out files that belong to SharePoint
    map_keys = set(gcs_map.keys())
    gcs_set = set(gcs_files)
    
    # Confluence files are those physically in GCS that are not registered under SharePoint
    confluence_gcs_set = {f for f in gcs_set if f not in sharepoint_keys}
    
    missing_in_gcs = map_keys - confluence_gcs_set
    extra_in_gcs = confluence_gcs_set - map_keys
    
    print("\n" + "-" * 40)
    print("VERIFICATION RESULTS:")
    print("-" * 40)
    
    if len(map_keys) == len(confluence_gcs_set) and not missing_in_gcs:
        print("✅ SUCCESS: Confluence map count and GCS physical file count match perfectly!")
        print(f"   Total Ingested Documents: {len(confluence_gcs_set)}")
    else:
        print("⚠️ MISMATCH DETECTED!")
        print(f"   Registered in Map: {len(map_keys)}")
        print(f"   Found in GCS Bucket (Confluence): {len(confluence_gcs_set)}")
        
        if missing_in_gcs:
            print(f"\n❌ {len(missing_in_gcs)} file(s) registered in map but MISSING from GCS bucket:")
            for item in sorted(missing_in_gcs):
                print(f"   - {item} (URL: {gcs_map[item]})")
                
        if extra_in_gcs:
            print(f"\nℹ️ {len(extra_in_gcs)} file(s) present in GCS bucket but NOT registered in current local map:")
            for item in sorted(extra_in_gcs):
                print(f"   - {item}")
                
    print("=" * 60)

if __name__ == "__main__":
    main()
