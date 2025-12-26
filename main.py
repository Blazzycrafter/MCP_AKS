from fastmcp import FastMCP
import requests

mcp = FastMCP("AKS")

@mcp.tool
def search(name: str) -> dict:
    url = "https://www.allkeyshop.com/api/v2-1-250304/vaks.php"
    params = {
        "action": "CatalogV2",
        "locale": "en",
        "currency": "EUR",
        "search_name": name,
        "price_mode": "price_card",
        "sort_field": "popularity_score",
        "sort_order": "desc",
        "fields": "id,name,offers.price,offers.merchant.name",
    }

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()

    # Optional: API-Status prüfen
    if data.get("status") != "success":
        return {
            "status": data.get("status", "error"),
            "errors": data.get("errors", []),
            "products": {},
        }

    products = data.get("products", [])

    # Sauberes Dictionary: product_id -> Produktdaten
    d: dict[int, dict] = {}

    for p in products:
        pid = p.get("id")
        pname = p.get("name")

        offers_clean = []
        for o in p.get("offers", []) or []:
            merchant = (o.get("merchant") or {}).get("name")
            price = o.get("price")
            if merchant is None or price is None:
                continue
            offers_clean.append({"merchant": merchant, "price": price})

        # Optional: nach Preis sortieren (günstigstes zuerst)
        offers_clean.sort(key=lambda x: x["price"])

        d[pid] = {
            "id": pid,
            "name": pname,
            "offers": offers_clean,
            "best_offer": offers_clean[0] if offers_clean else None,
        }

    return {
        "query": name,
        "count": len(d),
        "pagination": data.get("pagination", {}),
        "products": d,
    }


if __name__ == "__main__":
    # Start an HTTP server on port 8000
    mcp.run(transport="http", host="0.0.0.0", port=8000)
