# Commands Quick Reference

## Chrome Automation (Port 18010)

```bash
# List tabs
curl -s -H "Authorization: Bearer $(cat ~/muse-browser-mcp/daemon_token)" \
  -H "Content-Type: application/json" \
  -d '{"method":"tabs.list","params":{}}' \
  http://127.0.0.1:18010/tool

# Navigate tab 123 to example.com
curl -s -H "Authorization: Bearer $(cat ~/muse-browser-mcp/daemon_token)" \
  -H "Content-Type: application/json" \
  -d '{"method":"page.navigate","params":{"tabId":123,"url":"https://example.com"}}' \
  http://127.0.0.1:18010/tool

# Screenshot tab
curl -s -H "Authorization: Bearer $(cat ~/muse-browser-mcp/daemon_token)" \
  -H "Content-Type: application/json" \
  -d '{"method":"page.screenshot","params":{"tabId":123}}' \
  http://127.0.0.1:18010/tool
```

## Obscura (Port 9222)

```bash
# Start main profile
python ~/muse-browser-mcp/obscura_profiles.py main

# Start temp profile
python ~/muse-browser-mcp/obscura_profiles.py temp

# Sync Chrome cookies
python ~/muse-browser-mcp/obscura_profiles.py sync

# List running
python ~/muse-browser-mcp/obscura_profiles.py list
```

## PC Agent (Port 18011)

```bash
# Health check
curl -s http://127.0.0.1:18011/health

# Click at (500, 300)
curl -s -H "Content-Type: application/json" \
  -d '{"x":500,"y":300}' http://127.0.0.1:18011/click

# Type text
curl -s -H "Content-Type: application/json" \
  -d '{"text":"hello world"}' http://127.0.0.1:18011/type

# Press Enter
curl -s -H "Content-Type: application/json" \
  -d '{"key":"enter"}' http://127.0.0.1:18011/press

# Screenshot
curl -s -H "Content-Type: application/json" \
  -d '{}' http://127.0.0.1:18011/screenshot

# Open Chrome
curl -s -H "Content-Type: application/json" \
  -d '{"app":"chrome"}' http://127.0.0.1:18011/open

# Scroll down
curl -s -H "Content-Type: application/json" \
  -d '{"amount":-500}' http://127.0.0.1:18011/scroll
```

## Python Examples

```python
# PC Agent click
import urllib.request, json
def pc_click(x, y):
    req = urllib.request.Request("http://127.0.0.1:18011/click",
        data=json.dumps({"x": x, "y": y}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())

# PC Agent screenshot
def pc_shot():
    import base64
    req = urllib.request.Request("http://127.0.0.1:18011/screenshot",
        data=json.dumps({}).encode(),
        headers={"Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req).read())
    return base64.b64decode(data["image"])

# Chrome tab screenshot via daemon
def chrome_shot(tab_id):
    import base64
    tok = open(os.path.expandvars(r"%USERPROFILE%/muse-browser-mcp/daemon_token")).read().strip()
    data = json.dumps({"method": "page.screenshot", "params": {"tabId": tab_id}}).encode()
    req = urllib.request.Request("http://127.0.0.1:18010/tool", data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
    result = json.loads(urllib.request.urlopen(req).read())
    return base64.b64decode(result["image"])
```
