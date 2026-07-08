import urllib.request

def test_image_proxy():
    # Test loading one of the known image filenames we saw in the GCS bucket list
    for image_name in ["image-20260609-094553.png", "architecture-bank-alpha.png"]:
        url = f"http://localhost:8020/api/images/{image_name}"
        
        print(f"Testing local GCS image proxy URL: {url} ...")
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req) as response:
                print(f"Response status: {response.status}")
                print(f"Content Type: {response.headers.get('Content-Type')}")
                content_length = len(response.read())
                print(f"Received image size: {content_length} bytes")
                if content_length > 100:
                    print("SUCCESS: GCS image proxy successfully fetched and served the authenticated image bytes!")
                else:
                    print("WARNING: Image content size is too small, check proxy.")
        except Exception as e:
            print(f"ERROR: Image proxy test failed for {image_name}: {e}")

if __name__ == "__main__":
    test_image_proxy()
