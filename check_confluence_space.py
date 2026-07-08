import os
import sys
import httpx
from pathlib import Path
from dotenv import load_dotenv

def test_api():
    print("=" * 60)
    print("CONFLUENCE SPACE & PAGE VERIFICATION TOOL")
    print("=" * 60)

    # Resolve workspace paths and load .env configurations
    current_dir = Path(__file__).resolve().parent
    workspace_dir = current_dir.parent
    
    # Try current directory first, then parent directory
    load_dotenv(current_dir / ".env")
    load_dotenv(workspace_dir / ".env")

    # 1. Read configurations
    conf_url = os.getenv("CONFLUENCE_URL")
    conf_user = os.getenv("CONFLUENCE_USERNAME")
    conf_token = os.getenv("CONFLUENCE_API_TOKEN")

    if not conf_url:
        print("[Error] CONFLUENCE_URL is not set in environment or .env file.")
        return False
    if not conf_user or not conf_token:
        print("[Error] CONFLUENCE_USERNAME and CONFLUENCE_API_TOKEN are not set.")
        print("Please ensure your Basic Auth credentials are set in your environment or .env file.")
        return False

    base_api = f"{conf_url.rstrip('/')}/wiki/rest/api"
    auth = (conf_user, conf_token)

    print(f"Base Site: {conf_url}")
    print(f"API Username: {conf_user}")
    print(f"API Base URL: {base_api}")
    print("-" * 60)

    # 2. Test base connection & List visible spaces
    print("Testing connection to Confluence REST API...")
    try:
        resp = httpx.get(f"{base_api}/space", auth=auth, params={"limit": 100}, timeout=15)
        if resp.status_code == 401:
            print("[Error] 401 Unauthorized: Invalid username or API token.")
            return False
        elif resp.status_code == 403:
            print("[Error] 403 Forbidden: Your account is blocked from listing spaces.")
            return False
        
        resp.raise_for_status()
        spaces_data = resp.json().get("results", [])
        print(f"Connection Successful! Server returned {len(spaces_data)} accessible space(s) to this account.")
    except Exception as e:
        print(f"[Error] Failed to connect to Confluence REST API: {e}")
        return False

    # 3. List all accessible space keys
    accessible_keys = [s["key"] for s in spaces_data]
    print(f"Accessible Space Keys: {accessible_keys}")
    print("-" * 60)

    # 4. Determine target space key to verify
    target_space = "CVS"
    if len(sys.argv) > 1:
        target_space = sys.argv[1].upper()
    else:
        env_spaces = os.getenv("CONFLUENCE_SPACES")
        if env_spaces:
            first_space = [s.strip() for s in env_spaces.split(",") if s.strip()]
            if first_space:
                target_space = first_space[0]
    
    print(f"Checking Space: '{target_space}'")
    
    # Query Space Metadata
    try:
        space_resp = httpx.get(f"{base_api}/space/{target_space}", auth=auth, timeout=15)
        if space_resp.status_code == 404:
            print(f"\n❌ [Space Not Found] Space '{target_space}' returned 404 Not Found.")
            print("   -> Why this happens: Either the space key is typoed, or the configured API account")
            print("      lacks 'View' permissions to this specific space.")
            print("   -> Suggested fix: Ask an administrator of the space to grant 'View' permissions")
            print(f"      to the technical user '{conf_user}' in Space Settings -> Permissions.")
            return False
        
        space_resp.raise_for_status()
        space_info = space_resp.json()
        print(f"✅ Space '{target_space}' exists!")
        print(f"   Name: {space_info.get('name')}")
        print(f"   Type: {space_info.get('type')}")
        
    except Exception as e:
        print(f"[Error] Failed to fetch space metadata: {e}")
        return False

    # 5. List contents (first 10 pages) of the space
    print("-" * 60)
    print(f"Listing pages in space '{target_space}'...")
    try:
        pages_resp = httpx.get(
            f"{base_api}/content",
            auth=auth,
            params={
                "spaceKey": target_space,
                "type": "page",
                "limit": 10,
                "expand": "version"
            },
            timeout=15
        )
        pages_resp.raise_for_status()
        pages_data = pages_resp.json().get("results", [])
        
        if not pages_data:
            print(f"ℹ️ Space '{target_space}' is empty or contains no viewable pages.")
        else:
            print(f"✅ Successfully fetched first {len(pages_data)} page(s) in space '{target_space}':")
            print(f"{'Page ID':<15} | {'Version':<7} | {'Page Title'}")
            print("-" * 60)
            for page in pages_data:
                pid = page.get("id")
                title = page.get("title")
                version = page.get("version", {}).get("number", 1)
                print(f"{pid:<15} | v{version:<5} | {title}")
                
    except Exception as e:
        print(f"❌ [Error] Failed to fetch pages for space '{target_space}': {e}")
        print("   If you see a 404 error here but the space check above worked, it indicates a page-level permission restriction.")

    print("=" * 60)
    return True

if __name__ == "__main__":
    test_api()
