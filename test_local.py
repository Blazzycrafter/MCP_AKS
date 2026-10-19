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


def call_search(sid, arguments):
    sid, r = mcp_call("tools/call", {"name": "search", "arguments": arguments}, sid)
    return sid, r.get("result", {})


def structured(result):
    return result.get("structuredContent", {})


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
    # New optional parameters must be advertised in the schema.
    for param in ("page", "per_page", "currency", "sort", "order",
                  "exclude_accounts", "platform"):
        assert param in schema, f"missing tool param: {param}"
    assert tools[0].get("description"), "tool must have a description"

    # normal search — new list-based format
    sid, res = call_search(sid, {"name": "minecraft"})
    assert res.get("isError") is not True, res
    sc = structured(res)
    assert sc.get("status") == "success", sc
    assert sc.get("count", 0) > 0, sc
    assert isinstance(sc.get("products"), list), sc
    assert sc.get("currency") == "eur", sc
    assert isinstance(sc.get("pagination"), dict), sc
    assert "warnings" in sc, sc
    first = sc["products"][0]
    assert first.get("listing_kind") in ("key", "account", "gift"), first
    assert "best_offer" in first and "link" in first, first
    if first["best_offer"]:
        assert "buy_url" in first["best_offer"], first
    print("[OK] search('minecraft') -> count=%d, currency=%s" % (sc.get("count"), sc.get("currency")))

    # pagination + currency + sort parameters
    sid, res2 = call_search(sid, {"name": "minecraft", "page": 1, "per_page": 5,
                                  "currency": "usd", "sort": "price", "order": "asc"})
    sc2 = structured(res2)
    assert sc2.get("status") == "success", sc2
    assert sc2.get("currency") == "usd", sc2
    assert sc2.get("pagination", {}).get("per_page") == 5, sc2
    assert len(sc2.get("products", [])) <= 5, sc2
    print("[OK] page/per_page/currency/sort mapping")

    # exclude_accounts and platform filters must not error
    sid, res3 = call_search(sid, {"name": "god of war", "exclude_accounts": True})
    sc3 = structured(res3)
    assert sc3.get("status") == "success", sc3
    assert all(p.get("listing_kind") != "account" for p in sc3.get("products", [])), sc3
    sid, res4 = call_search(sid, {"name": "god of war", "platform": "ps5"})
    sc4 = structured(res4)
    assert sc4.get("status") == "success", sc4
    print("[OK] exclude_accounts + platform filters")

    # extended regex must accept real titles with comma/plus/registered marks
    sid, res5 = call_search(sid, {"name": "Warhammer 40,000"})
    sc5 = structured(res5)
    assert sc5.get("status") in ("success", "failed"), sc5
    print("[OK] 'Warhammer 40,000' accepted by input validation")

    # overlong name must be rejected by the schema
    sid, r4 = mcp_call("tools/call", {"name": "search", "arguments": {"name": "a" * 200}}, sid)
    assert "error" in r4 or r4.get("result", {}).get("isError") is True, r4
    print("[OK] overlong name rejected")

    # injection-style string must be rejected by the schema
    sid, r5 = mcp_call("tools/call", {"name": "search", "arguments": {"name": "drop; table"}}, sid)
    assert "error" in r5 or r5.get("result", {}).get("isError") is True, r5
    print("[OK] bad-chars rejected")

    # whitespace-only name must be rejected (normalized to empty)
    sid, res6 = call_search(sid, {"name": "   "})
    assert res6.get("isError") is not True, res6
    assert structured(res6).get("status") == "invalid_input", res6
    print("[OK] whitespace-only name rejected")

    print("[DONE] all local smoke tests passed")


if __name__ == "__main__":
    main()
