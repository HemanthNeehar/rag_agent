import os
import re
import sys
import httpx
from pathlib import Path
from dotenv import load_dotenv

workspace_dir = Path(__file__).parent.parent
load_dotenv(workspace_dir / ".env")

sys.path.append(str(workspace_dir))
from rag_agent.ingest_sharepoint_gcs import get_msal_token

def inspect():
    token = get_msal_token()
    if not token:
        print("[Error] Failed to get MSAL token.")
        return
        
    headers = {"Authorization": f"Bearer {token}"}
    base_url = "https://graph.microsoft.com/v1.0"
    
    # Discover sites
    search_query = os.getenv("SHAREPOINT_SITE_SEARCH_QUERY", "*")
    url = f"{base_url}/sites?search={search_query}"
    resp = httpx.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    sites = resp.json().get("value", [])
    
    site_filter = [s.strip() for s in os.getenv("SHAREPOINT_SITES_LIST", "").split(",") if s.strip()]
    if site_filter:
        sites = [s for s in sites if s.get("name") in site_filter or s.get("displayName") in site_filter]

    print(f"\nScanning {len(sites)} sites for PPTX files:")
    for site in sites:
        site_id = site.get("id")
        site_name = site.get("name", "Unknown")
        print(f"\n--- Site: {site_name} ({site_id}) ---")
        
        # Get drives
        drives_url = f"{base_url}/sites/{site_id}/drives"
        d_resp = httpx.get(drives_url, headers=headers, timeout=30)
        d_resp.raise_for_status()
        drives = d_resp.json().get("value", [])
        
        for drive in drives:
            drive_id = drive.get("id")
            folder_queue = [("root", "")]
            
            while folder_queue:
                folder_id, rel_path = folder_queue.pop(0)
                if folder_id == "root":
                    children_url = f"{base_url}/drives/{drive_id}/root/children"
                else:
                    children_url = f"{base_url}/drives/{drive_id}/items/{folder_id}/children"
                    
                c_resp = httpx.get(children_url, headers=headers, timeout=30)
                if c_resp.status_code != 200:
                    continue
                children = c_resp.json().get("value", [])
                
                for item in children:
                    item_name = item.get("name")
                    item_id = item.get("id")
                    if "folder" in item:
                        folder_queue.append((item_id, f"{rel_path}/{item_name}"))
                    elif item_name.lower().endswith(".pptx"):
                        print(f"\nFound PPTX file: {item_name}")
                        print(f"  Graph Item ID: {item_id}")
                        print(f"  Reported Size: {item.get('size')} bytes")
                        print(f"  File Facet details: {item.get('file')}")
                        print(f"  Has downloadUrl: {bool(item.get('@microsoft.graph.downloadUrl'))}")
                        print(f"  Web URL: {item.get('webUrl')}")

if __name__ == "__main__":
    inspect()
