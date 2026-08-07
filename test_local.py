"""Local smoke test for the AKS MCP server (no OAuth, talks to localhost:8000)."""
import json, urllib.request

BASE = "http://127.0.0.1:8000/mcp"


def mcp_call(method, params, sid=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "mcp-protocol-version": "2024-11-05"}
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(BASE, data=body, headers=h, method="POST")
    resp = urllib.request.urlopen(req, timeout=60)
    nsid = resp.headers.get("Mcp-Session-Id")
    event_data = {}
    for raw_line in resp:
        line = raw_line.decode("utf-8", "replace")
        if line.startswith("data:"):
            try:
                obj = json.loads(line[5:].strip())
            except Exception:
                continue
            if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                event_data = obj
                break
        if not line.strip() and event_data:
            break
    return nsid, event_data


sid, r1 = mcp_call("initialize", {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "clientInfo": {"name": "hermes-local-test", "version": "1.0"}})
print("[*] initialize:", json.dumps(r1.get("result", {}).get("serverInfo", {})))

initnote = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
     "mcp-protocol-version": "2024-11-05"}
if sid:
    h["Mcp-Session-Id"] = sid
try:
    urllib.request.urlopen(urllib.request.Request(BASE, data=initnote, headers=h, method="POST"), timeout=60).read()
except Exception:
    pass

sid, r2 = mcp_call("tools/list", {}, sid)
tools = r2.get("result", {}).get("tools", [])
print("[*] TOOLS:", [t["name"] for t in tools])
for t in tools:
    print("    -", t["name"], ":", (t.get("description") or "")[:90])
    print("      schema:", json.dumps(t.get("inputSchema", {}).get("properties", {})))

# call search with a normal query
sid, r3 = mcp_call("tools/call", {"name": "search", "arguments": {"name": "minecraft"}}, sid)
res3 = r3.get("result", {})
print("[*] search('minecraft') status:", res3.get("structuredContent", {}).get("status") if res3.get("structuredContent") else res3.get("content"))
sc = res3.get("structuredContent", {})
print("    count:", sc.get("count"), "| query:", sc.get("query"))

# negative test: injection-ish input must be rejected by the schema
sid, r4 = mcp_call("tools/call", {"name": "search", "arguments": {"name": "a" * 200}}, sid)
print("[*] overlong-name rejected:", "error" in r4 or "isError" in str(r4.get("result", {})))

sid, r5 = mcp_call("tools/call", {"name": "search", "arguments": {"name": "drop; table"}}, sid)
print("[*] bad-chars rejected:", "error" in r5 or "isError" in str(r5.get("result", {})))
print("[DONE]")
