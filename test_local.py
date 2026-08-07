"""Local smoke test for the AKS MCP server (no OAuth, talks to localhost:8000).

Run:  python test_local.py
Exits non-zero if any assertion fails, so it is CI-friendly.
"""
import json
import itertools
import urllib.request

BASE = "http://127.0.0.1:8000/mcp"
_REQUEST_IDS = itertools.count(1)


def mcp_call(method, params, sid=None):
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": next(_REQUEST_IDS),
        "method": method,
        "params": params,
    }).encode()
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2024-11-05",
    }
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(BASE, data=body, headers=h, method="POST")
    resp = urllib.request.urlopen(req, timeout=60)
    nsid = resp.headers.get("Mcp-Session-Id")
    if nsid:  # preserve an already-established session across calls
        sid = nsid
    ctype = resp.headers.get("Content-Type", "")
    # Plain JSON vs SSE stream: parse accordingly.
    if "text/event-stream" in ctype:
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
        return sid, event_data
    return sid, json.load(resp)


def main():
    sid, r1 = mcp_call("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "hermes-local-test", "version": "1.0"}})
    assert r1.get("result", {}).get("serverInfo", {}).get("name") == "AKS", r1

    initnote = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "mcp-protocol-version": "2024-11-05"}
    if sid:
        h["Mcp-Session-Id"] = sid
    # initialized notification must succeed (no blanket suppression)
    with urllib.request.urlopen(
        urllib.request.Request(BASE, data=initnote, headers=h, method="POST"), timeout=60
    ) as _:
        pass

    sid, r2 = mcp_call("tools/list", {}, sid)
    tools = r2.get("result", {}).get("tools", [])
    assert [t["name"] for t in tools] == ["search"], tools
    schema = tools[0].get("inputSchema", {}).get("properties", {})
    assert "name" in schema, schema
    assert tools[0].get("description"), "tool must have a description"

    # normal search
    sid, r3 = mcp_call("tools/call", {"name": "search", "arguments": {"name": "minecraft"}}, sid)
    res3 = r3.get("result", {})
    assert res3.get("isError") is not True, res3
    sc = res3.get("structuredContent", {})
    assert sc.get("status") == "success", sc
    assert sc.get("count", 0) > 0, sc
    print("[OK] search('minecraft') -> count=%d" % sc.get("count"))

    # overlong name must be rejected by the schema
    sid, r4 = mcp_call("tools/call", {"name": "search", "arguments": {"name": "a" * 200}}, sid)
    assert "error" in r4 or r4.get("result", {}).get("isError") is True, r4
    print("[OK] overlong name rejected")

    # injection-style string must be rejected by the schema
    sid, r5 = mcp_call("tools/call", {"name": "search", "arguments": {"name": "drop; table"}}, sid)
    assert "error" in r5 or r5.get("result", {}).get("isError") is True, r5
    print("[OK] bad-chars rejected")

    # whitespace-only name must be rejected (normalized to empty)
    sid, r6 = mcp_call("tools/call", {"name": "search", "arguments": {"name": "   "}}, sid)
    res6 = r6.get("result", {})
    assert res6.get("isError") is not True, res6
    assert res6.get("structuredContent", {}).get("status") == "invalid_input", res6
    print("[OK] whitespace-only name rejected")

    print("[DONE] all local smoke tests passed")


if __name__ == "__main__":
    main()
