import requests
import json

url = "http://localhost:8020/api/stream"
params = {
    "query": "Explain Google Next Announcements Overview"
}

print(f"Sending request to {url} with query='{params['query']}'...")
response = requests.get(url, params=params, stream=True)

print(f"Response status: {response.status_code}")
print("Streamed tokens received:")
for line in response.iter_lines():
    if line:
        decoded_line = line.decode('utf-8')
        if decoded_line.startswith("data: "):
            data_str = decoded_line[6:]
            try:
                data = json.loads(data_str)
                if "token" in data:
                    print(data["token"], end="", flush=True)
                elif "done" in data:
                    print("\n[Done received]")
            except Exception as e:
                print(f"\n[Error parsing JSON: {e}]")
