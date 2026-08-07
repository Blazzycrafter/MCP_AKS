"""
FastMCP server "AKS" — Game-key price aggregator (AllKeyShop API).

Hardened for production use:
  * Pydantic-validated tool input (no injection vector, bounded length).
  * Explicit tool description so the client/LLM knows what it does.
  * In-process rate limiting to protect the upstream API key/quota.
  * Strict timeouts + safe error envelope (never leak upstream internals).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Annotated
from fastmcp import FastMCP
from pydantic import Field
import requests

mcp = FastMCP("AKS")

UPSTREAM_URL = "https://www.allkeyshop.com/api/v2-1-250304/vaks.php"
REQUEST_TIMEOUT = 20
# Per-process rate limit: max 10 calls per 10s window (protects upstream quota).
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 10.0
_call_times: deque[float] = deque()


def _rate_limited() -> bool:
    now = time.monotonic()
    while _call_times and now - _call_times[0] > RATE_LIMIT_WINDOW:
        _call_times.popleft()
    if len(_call_times) >= RATE_LIMIT_MAX:
        return True
    _call_times.append(now)
    return False


@mcp.tool(
    name="search",
    description=(
        "Search aggregated game-key resellers via the AllKeyShop API for a game "
        "by name and return the offers grouped by product, each with merchant and "
        "price (cheapest offer first). Use for comparing game-key prices across "
        "stores (e.g. Driffle, Eneba, G2A, Kinguin, GAMIVO, PS Store, Nintendo eShop)."
    ),
)
def search(
    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=80,
            pattern=r"^[\w \-'.:!?&()]+$",
            description="Game name to look up, e.g. 'minecraft' or 'elden ring'.",
        ),
    ],
) -> dict:
    if _rate_limited():
        return {
            "status": "rate_limited",
            "message": "Too many requests in a short window. Slow down.",
            "products": {},
        }

    params = {
        "action": "CatalogV2",
        "locale": "en",
        "currency": "EUR",
        "search_name": name.strip(),
        "price_mode": "price_card",
        "sort_field": "popularity_score",
        "sort_order": "desc",
        "fields": "id,name,offers.price,offers.merchant.name",
    }

    try:
        response = requests.get(UPSTREAM_URL, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        return {
            "status": "upstream_error",
            "message": "Failed to reach the price aggregator.",
            "detail": type(exc).__name__,
            "products": {},
        }
    except ValueError:
        return {
            "status": "upstream_error",
            "message": "Malformed response from the price aggregator.",
            "products": {},
        }

    if data.get("status") != "success":
        return {
            "status": data.get("status", "error"),
            "errors": data.get("errors", []),
            "products": {},
        }

    products = data.get("products", [])
    result: dict[int, dict] = {}

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

        offers_clean.sort(key=lambda x: x["price"])

        result[pid] = {
            "id": pid,
            "name": pname,
            "offers": offers_clean,
            "best_offer": offers_clean[0] if offers_clean else None,
        }

    return {
        "status": "success",
        "query": name,
        "count": len(result),
        "pagination": data.get("pagination", {}),
        "products": result,
    }


if __name__ == "__main__":
    # Streamable HTTP server on port 8000 (Horizon/Prefect proxy fronts this).
    mcp.run(transport="http", host="0.0.0.0", port=8000)
