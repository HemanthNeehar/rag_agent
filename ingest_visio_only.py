"""
Targeted Visio (.vsdx) Ingestion & LLM Summarization Script

Searches SharePoint via Microsoft Graph API specifically for .vsdx files across all sites/drives,
downloads them locally, parses shape text spatially (PinX/PinY Top->Bottom, Left->Right),
extracts embedded thumbnail preview images, calls Gemini 2.5 Flash Vision for architectural summaries,
and uploads the enriched markdown files to GCS.
"""

import os
import re
import json
import httpx
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from dotenv import load_dotenv

rag_agent_dir = Path(__file__).parent.resolve()
load_dotenv(rag_agent_dir / ".env", override=True)

from rag_agent.ingest_sharepoint_gcs import (
    get_msal_token as get_graph_access_token,
    _parse_vsdx_locally,
    upload_file_to_gcs,
    clean_filename,
    fetch_sharepoint_item_permissions,
)

def run_visio_only_ingestion():
    print("=" * 70)
    print("STARTING TARGETED VISIO (.vsdx) INGESTION & LLM SUMMARIZATION")
    print("=" * 70)

    token = get_graph_access_token()
    if not token:
        print("[Error] Could not acquire Graph API access token. Aborting.")
        return

    headers = {"Authorization": f"Bearer {token}"}
    base_url = "https://graph.microsoft.com/v1.0"
    gcs_bucket_name = os.getenv("RAG_GCS_BUCKET_NAME", "multi-agent-sdlc")
    gcs_prefix = f"gs://{gcs_bucket_name}"

    # State maps
    sp_map_path = rag_agent_dir / "gcs_sharepoint_map.json"
    sp_perms_path = rag_agent_dir / "gcs_sharepoint_permissions_map.json"

    sp_map = {}
    if sp_map_path.exists():
        try:
            sp_map = json.loads(sp_map_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    sp_perms = {}
    if sp_perms_path.exists():
        try:
            sp_perms = json.loads(sp_perms_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # 1. Fetch sites
    site_filter = [s.strip() for s in os.getenv("SHAREPOINT_SITES_LIST", "").split(",") if s.strip()]
    env_my_site = os.getenv("SHAREPOINT_SINGLE_SITE_PATH")
    my_tenant = os.getenv("SHAREPOINT_TENANT", "your-organization.sharepoint.com")

    sites = []
    try:
        if env_my_site:
            url = f"{base_url}/sites/{my_tenant}:{env_my_site}"
            resp = httpx.get(url, headers=headers, timeout=60)
            if resp.status_code == 200:
                sites = [resp.json()]
        elif site_filter:
            for site_name_or_path in site_filter:
                site_path = site_name_or_path if site_name_or_path.startswith("/") else f"/sites/{site_name_or_path}"
                url = f"{base_url}/sites/{my_tenant}:{site_path}"
                resp = httpx.get(url, headers=headers, timeout=60)
                if resp.status_code == 200:
                    sites.append(resp.json())
        else:
            url = f"{base_url}/sites?search=*"
            resp = httpx.get(url, headers=headers, timeout=60)
            if resp.status_code == 200:
                sites = resp.json().get("value", [])
    except Exception as site_err:
        print(f"[Warning] Site discovery error: {site_err}")

    if not sites:
        url = f"{base_url}/sites/{my_tenant}:/sites/YourApplicationSite"
        resp = httpx.get(url, headers=headers, timeout=60)
        if resp.status_code == 200:
            sites = [resp.json()]

    print(f"\n[1/4] Discovered {len(sites)} SharePoint sites. Crawling drives for .vsdx Visio diagrams...\n")

    vsdx_count = 0
    temp_dir = rag_agent_dir / "temp_vsdx_processing"
    temp_dir.mkdir(exist_ok=True)

    for site in sites:
        site_id = site.get("id")
        site_name = site.get("displayName") or site.get("name", "UnknownSite")
        web_url = site.get("webUrl", "")

        try:
            drives_resp = httpx.get(f"{base_url}/sites/{site_id}/drives", headers=headers, timeout=30.0)
            drives = drives_resp.json().get("value", []) if drives_resp.status_code == 200 else []
            
            for drive in drives:
                drive_id = drive.get("id")
                folder_queue = [("root", "")]

                while folder_queue:
                    folder_id, rel_path = folder_queue.pop(0)
                    children_url = f"{base_url}/drives/{drive_id}/root/children" if folder_id == "root" else f"{base_url}/drives/{drive_id}/items/{folder_id}/children"

                    try:
                        resp = httpx.get(children_url, headers=headers, timeout=30.0)
                        if resp.status_code != 200:
                            continue
                        items = resp.json().get("value", [])
                        for item in items:
                            item_id = item.get("id")
                            item_name = item.get("name", "")
                            
                            if "folder" in item:
                                sub_path = f"{rel_path}/{item_name}" if rel_path else item_name
                                folder_queue.append((item_id, sub_path))
                            elif item_name.lower().endswith(".vsdx"):
                                vsdx_count += 1
                                item_web_url = item.get("webUrl", web_url)
                                print(f"➜ [{vsdx_count}] Found Visio diagram: '{item_name}' in site '{site_name}'")

                                # Download vsdx file locally
                                download_url = item.get("@microsoft.graph.downloadUrl")
                                local_vsdx_path = temp_dir / clean_filename(item_name)
                                downloaded = False
                                
                                if download_url:
                                    try:
                                        r = httpx.get(download_url, follow_redirects=True, timeout=60.0)
                                        if r.status_code == 200:
                                            local_vsdx_path.write_bytes(r.content)
                                            downloaded = True
                                    except Exception as dl_err:
                                        print(f"      [Warning] Direct downloadUrl failed for {item_name}: {dl_err}")

                                if not downloaded:
                                    content_url = f"{base_url}/drives/{drive_id}/items/{item_id}/content"
                                    try:
                                        r = httpx.get(content_url, headers=headers, follow_redirects=True, timeout=90.0)
                                        if r.status_code == 200:
                                            local_vsdx_path.write_bytes(r.content)
                                            downloaded = True
                                    except Exception as dl_err:
                                        print(f"      [Error] Graph API content stream failed for {item_name}: {dl_err}")

                                if not downloaded or not local_vsdx_path.exists():
                                    print(f"      [Error] Skipping {item_name}: Could not download file.")
                                    continue

                                # Parse VSDX with spatial sorting + multimodal vision narrative
                                parsed_markdown = _parse_vsdx_locally(local_vsdx_path)

                                perms = fetch_sharepoint_item_permissions(base_url, drive_id, item_id, headers)
                                perms["site_name"] = site_name
                                perms["site_url"] = web_url

                                clean_title = clean_filename(f"v2_sp_{site_name}_{item_name}")
                                md_filename = clean_title.replace(".vsdx.md", ".md") if clean_title.endswith(".vsdx.md") else clean_title

                                header_block = (
                                    f"source_url: {item_web_url}\n"
                                    f"title: {item_name}\n"
                                    f"source_system: SharePoint\n"
                                    f"site_name: {site_name}\n"
                                    f"restricted: {perms.get('restricted', False)}\n"
                                    f"allowed_groups: {','.join(perms.get('allowed_groups', []))}\n"
                                    f"allowed_users: {','.join(perms.get('allowed_users', []))}\n\n"
                                )
                                final_content = header_block + parsed_markdown + f"\n\n---\ndoc_id: sharepoint_vsdx_{item_id}\nsource_url: {item_web_url}\n"

                                local_md_path = temp_dir / md_filename
                                local_md_path.write_text(final_content, encoding="utf-8")

                                gcs_blob_uri = f"{gcs_prefix}/{md_filename}"
                                if upload_file_to_gcs(str(local_md_path), gcs_blob_uri):
                                    print(f"      ✔ Uploaded {md_filename} to {gcs_blob_uri}")
                                    sp_map[gcs_blob_uri] = item_web_url
                                    sp_perms[md_filename] = perms

                                if local_vsdx_path.exists():
                                    try: local_vsdx_path.unlink()
                                    except: pass

                    except Exception as crawl_err:
                        print(f"      [Warning] Folder crawl error in drive {drive_id}: {crawl_err}")

        except Exception as e:
            print(f"   [Warning] Drive scan failed for site {site_name}: {e}")

    # 2. Update state map files
    print(f"\n[2/4] Updating SharePoint URL & Permissions Maps ({vsdx_count} Visio files processed)...")
    sp_map_path.write_text(json.dumps(sp_map, indent=2), encoding="utf-8")
    sp_perms_path.write_text(json.dumps(sp_perms, indent=2), encoding="utf-8")

    upload_file_to_gcs(str(sp_map_path), f"{gcs_prefix}/gcs_sharepoint_map.json")
    upload_file_to_gcs(str(sp_perms_path), f"{gcs_prefix}/gcs_sharepoint_permissions_map.json")

    # Clean up temp folder
    try:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass

    print("\n" + "=" * 70)
    print("TARGETED VISIO INGESTION COMPLETED SUCCESSFULLY!")
    print(f"Total Visio diagrams processed & enriched with Gemini summaries: {vsdx_count}")
    print("Next Step: Run push job to update RAG Corpus index:")
    print("   python -m rag_agent.push_rag_engine")
    print("=" * 70)

if __name__ == "__main__":
    run_visio_only_ingestion()
