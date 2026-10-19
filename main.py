"""
FastMCP server "AKS" — Game-key price aggregator (AllKeyShop API).

Hardened and enriched for production use:
  * Pydantic-validated tool input (no injection vector, bounded length).
  * Explicit, honest tool description so the client/LLM knows what it does.
  * In-process rate limiting to protect the upstream API key/quota.
  * Strict timeouts + safe error envelope (never leak upstream internals).
  * Rich, client-friendly output: products as an ordered list, best offer per
    product, shop links, currency, pagination and upstream warnings.
  * Defensive parsing: malformed products/offers or non-JSON (HTML) upstream
    responses never crash the server.

Design notes / heuristics are documented in README.md.
"""

from __future__ import annotations

import os
import re
import time
import threading
from collections import deque
from typing import Annotated, Any, Literal

import requests
from fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("AKS")

# --- Configuration (overridable via environment variables) -------------------
UPSTREAM_URL = os.environ.get(
    "AKS_UPSTREAM_URL",
    "https://www.allkeyshop.com/api/v2-1-250304/vaks.php",
)
REQUEST_TIMEOUT = float(os.environ.get("AKS_REQUEST_TIMEOUT", "20"))
# Per-process rate limit: max N calls per window (protects upstream quota).
RATE_LIMIT_MAX = int(os.environ.get("AKS_RATE_LIMIT_MAX", "10"))
RATE_LIMIT_WINDOW = float(os.environ.get("AKS_RATE_LIMIT_WINDOW", "10.0"))
SERVER_HOST = os.environ.get("AKS_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("AKS_PORT", "8000"))

# Mapping from the public, stable sort names to the upstream sort_field values.
SORT_FIELDS = {"popularity": "popularity_score", "price": "price", "name": "name"}
# Only these currencies are accepted by the upstream API.
CURRENCIES = ("eur", "usd", "gbp")

# Allowed characters in a search term. Extended beyond the original set so that
# real titles are accepted (comma, plus, slash, hash, ® and ™). The value is
# URL-encoded by requests, so these characters are safe to pass through.
NAME_PATTERN = r"^[\w \-'.:!?&(),+/#®™]+$"

# Reusable, schema-validated field type for the search term (kept as a module
# alias so it can be unit-tested with pydantic without a running server).
SearchName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=80,
        pattern=NAME_PATTERN,
        description=(
            "Game name to look up, e.g. 'minecraft', 'elden ring' or "
            "'Warhammer 40,000'."
        ),
    ),
]

# Fields requested from the upstream API. Only fields that the API itself lists
# as available (see `fields=...,bogus` warning) are requested — no invented
# fields. Upstream URL-encodes these; nothing here is user controlled.
UPSTREAM_FIELDS = ",".join(
    [
        "id",
        "name",
        "type",
        "link",
        "offers_count",
        "activation_platforms.name",
        "offers.id",
        "offers.price",
        "offers.buy_url",
        "offers.edition.name",
        "offers.region.name",
        "offers.region.display_name",
        "offers.stock_status",
        "offers.voucher.code",
        "offers.voucher.discount",
        "offers.merchant.name",
        "offers.merchant.id",
        "offers.merchant.type",
        "official_offers.price",
        "official_offers.merchant.name",
        "historical_low_offers.price",
        "historical_low_offers.date",
        "historical_low_offers.merchant.name",
    ]
)

_call_times: deque[float] = deque()
_rate_lock = threading.Lock()


def _rate_limited() -> bool:
    with _rate_lock:
        now = time.monotonic()
        while _call_times and now - _call_times[0] > RATE_LIMIT_WINDOW:
            _call_times.popleft()
        if len(_call_times) >= RATE_LIMIT_MAX:
            return True
        _call_times.append(now)
        return False


def reset_rate_limiter() -> None:
    """Clear the rate-limiter state (used by tests)."""
    with _rate_lock:
        _call_times.clear()


# --- Defensive parsing helpers ----------------------------------------------
def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_num(value: Any) -> int | float | None:
    # bool is a subclass of int — never treat True/False as a price.
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _listing_kind(
    name: str | None,
    region_name: str | None,
    upstream_type: str | None = None,
) -> str:
    """Classify a listing as ``key``, ``account`` or ``gift``.

    The upstream ``type`` field is authoritative when it carries a recognised
    value (``account``/``key``/``gift``, case-insensitive). Missing or unknown
    values (e.g. ``game``, ``dlc``) fall back to a heuristic:

    * "account" — the word "account" appears anywhere in the product name
      (e.g. "... Account Deluxe Edition", "... Account PS5"), not only as a
      trailing suffix.
    * "gift"    — the best offer's region name contains "Gift".
    * "key"     — everything else.
    """
    utype = (upstream_type or "").strip().lower()
    if utype in ("account", "key", "gift"):
        return utype

    lname = (name or "").strip().lower()
    if re.search(r"\baccounts?\b", lname):
        return "account"
    if region_name and "gift" in region_name.lower():
        return "gift"
    return "key"


def _build_offer(raw: Any, currency: str) -> dict | None:
    """Normalise a single upstream offer defensively."""
    offer = _as_dict(raw)
    if not offer:
        return None

    merchant = _as_dict(offer.get("merchant"))
    region = _as_dict(offer.get("region"))
    edition = _as_dict(offer.get("edition"))
    voucher = _as_dict(offer.get("voucher"))
    price = _as_num(offer.get("price"))

    voucher_out = None
    if voucher:
        code = _as_str(voucher.get("code"))
        discount = _as_num(voucher.get("discount"))
        if code is not None or discount is not None:
            voucher_out = {"code": code, "discount": discount}

    return {
        "merchant": _as_str(merchant.get("name")),
        "merchant_id": _as_num(merchant.get("id")),
        "price": price,
        "currency": currency,
        "buy_url": _as_str(offer.get("buy_url")),
        "edition": _as_str(edition.get("name")),
        "region": _as_str(region.get("name")),
        "region_display": _as_str(region.get("display_name")),
        "stock_status": _as_str(offer.get("stock_status")),
        "voucher": voucher_out,
        # A price of 0 (e.g. free-to-keep DLC via a store) must not be read as
        # a "bargain"; clients can use this flag to present it correctly.
        "is_free": price == 0,
    }


def _best_offer(offers: list, currency: str) -> dict | None:
    """Return the cheapest parseable offer, or the first parseable one.

    With `price_mode=price_card` the upstream normally returns exactly one
    offer per product; we still pick the minimum price defensively.
    """
    parsed: list[dict] = []
    for raw in offers:
        built = _build_offer(raw, currency)
        if built is not None:
            parsed.append(built)

    priced = [o for o in parsed if o["price"] is not None]
    if priced:
        return min(priced, key=lambda o: o["price"])
    return parsed[0] if parsed else None


def _official_price(official_offers: list) -> dict | None:
    for raw in official_offers:
        offer = _as_dict(raw)
        if not offer:
            continue
        merchant = _as_dict(offer.get("merchant"))
        return {
            "merchant": _as_str(merchant.get("name")),
            "price": _as_num(offer.get("price")),
        }
    return None


def _historical_low(items: list) -> dict | None:
    for raw in items:
        item = _as_dict(raw)
        if not item:
            continue
        merchant = _as_dict(item.get("merchant"))
        return {
            "merchant": _as_str(merchant.get("name")),
            "price": _as_num(item.get("price")),
            "date": _as_str(item.get("date")),
        }
    return None


def _activation_platforms(product: dict) -> list[str]:
    names: list[str] = []
    for raw in _as_list(product.get("activation_platforms")):
        name = _as_str(_as_dict(raw).get("name"))
        if name:
            names.append(name)
    return names


def _matches_platform(product: dict, platform: str) -> bool:
    """Case-insensitive, client-side platform filter (heuristic).

    Matches when the token appears in an activation-platform name, or as a
    whole word / suffix in the product name (e.g. "PS5", "Steam", "Xbox").
    This only filters the products on the current page — the upstream API
    exposes no server-side platform filter (see README / PR notes).
    """
    token = platform.strip().lower()
    if not token:
        return True

    for raw in _as_list(product.get("activation_platforms")):
        name = _as_str(_as_dict(raw).get("name"))
        if not name:
            continue
        lname = name.lower()
        # Whole-token match: "ea" must not match "Steam".
        if lname == token or token in re.findall(r"[a-z0-9]+", lname):
            return True
        # Multi-word tokens (e.g. "xbox one") match as a whole phrase.
        if " " in token and re.search(rf"\b{re.escape(token)}\b", lname):
            return True

    pname = (_as_str(product.get("name")) or "").lower()
    if pname.endswith(" " + token) or pname.endswith("-" + token):
        return True
    if token in re.findall(r"[a-z0-9]+", pname):
        return True
    if " " in token and re.search(rf"\b{re.escape(token)}\b", pname):
        return True
    return False


# --- Core logic (separated from the tool wrapper for offline unit tests) -----
def _search_impl(
    name: str,
    page: int = 1,
    per_page: int = 20,
    currency: str = "eur",
    sort: str = "popularity",
    order: str = "desc",
    exclude_accounts: bool = False,
    platform: str | None = None,
) -> dict:
    query = name.strip() if isinstance(name, str) else ""
    if not query:
        return {
            "status": "invalid_input",
            "message": "Search name must not be empty or whitespace only.",
            "products": [],
        }

    if _rate_limited():
        return {
            "status": "rate_limited",
            "message": "Too many requests in a short window. Slow down.",
            "products": [],
        }

    params = {
        "action": "CatalogV2",
        "locale": "en",
        "currency": currency,
        "search_name": query,
        "price_mode": "price_card",
        "sort_field": SORT_FIELDS.get(sort, "popularity_score"),
        "sort_order": order,
        "pagenum": page,
        "per_page": per_page,
        "fields": UPSTREAM_FIELDS,
    }

    try:
        response = requests.get(UPSTREAM_URL, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        return {
            "status": "upstream_error",
            "message": "Failed to reach the price aggregator.",
            "detail": type(exc).__name__,
            "products": [],
        }

    # The upstream can return HTML (e.g. a 503 "Backend fetch failed" page).
    try:
        data = response.json()
    except ValueError:
        return {
            "status": "upstream_error",
            "message": "Malformed (non-JSON) response from the price aggregator.",
            "products": [],
        }

    if not isinstance(data, dict):
        return {
            "status": "upstream_error",
            "message": "Malformed response from the price aggregator.",
            "products": [],
        }

    warnings = _as_list(data.get("warnings"))

    if data.get("status") != "success":
        # Surface the upstream failure without leaking internal error text
        # (the module promises never to expose upstream internals).
        return {
            "status": _as_str(data.get("status")) or "error",
            "message": "The price aggregator could not complete the search.",
            "warnings": warnings,
            "products": [],
        }

    products_out: list[dict] = []
    for raw in _as_list(data.get("products")):
        product = _as_dict(raw)
        if not product:
            continue

        best = _best_offer(_as_list(product.get("offers")), currency)
        region_name = best["region"] if best else None
        kind = _listing_kind(
            _as_str(product.get("name")),
            region_name,
            _as_str(product.get("type")),
        )

        if exclude_accounts and kind == "account":
            continue
        if platform and not _matches_platform(product, platform):
            continue

        products_out.append(
            {
                "id": _as_num(product.get("id")),
                "name": _as_str(product.get("name")),
                "type": _as_str(product.get("type")),
                "link": _as_str(product.get("link")),
                "offers_count": _as_num(product.get("offers_count")),
                "activation_platforms": _activation_platforms(product),
                "best_offer": best,
                "official_price": _official_price(
                    _as_list(product.get("official_offers"))
                ),
                "historical_low": _historical_low(
                    _as_list(product.get("historical_low_offers"))
                ),
                "listing_kind": kind,
            }
        )

    return {
        "status": "success",
        "query": query,
        "currency": currency,
        "count": len(products_out),
        "pagination": _as_dict(data.get("pagination")),
        "warnings": warnings,
        "products": products_out,
    }


@mcp.tool(
    name="search",
    description=(
        "Search aggregated game-key resellers via the AllKeyShop API for a game "
        "by name and return matching products. Each product contains only the "
        "single best (cheapest) offer for the current price_card query; "
        "offers_count is the total number of offers across all merchants. "
        "Prices are in the requested currency (eur/usd/gbp). Products are "
        "returned as an ordered list. `listing_kind` (key/account/gift) uses the "
        "upstream product type when available, falling back to a name/region "
        "heuristic: use `exclude_accounts` to drop account listings. "
        "A best_offer with is_free=true means the price is 0 (e.g. free DLC) and "
        "is not a paid bargain. `platform` filters client-side by activation "
        "platform / product name and only affects the current page, since no "
        "server-side platform filter is known. Use for comparing game-key prices "
        "across stores (e.g. Driffle, Eneba, G2A, Kinguin, GAMIVO, PS Store, "
        "Nintendo eShop)."
    ),
)
def search(
    name: SearchName,
    page: Annotated[
        int,
        Field(ge=1, description="1-based result page (maps to upstream pagenum)."),
    ] = 1,
    per_page: Annotated[
        int,
        Field(ge=1, le=50, description="Results per page (1-50, default 20)."),
    ] = 20,
    currency: Annotated[
        Literal["eur", "usd", "gbp"],
        Field(description="Price currency. One of eur, usd, gbp."),
    ] = "eur",
    sort: Annotated[
        Literal["popularity", "price", "name"],
        Field(description="Sort field: popularity (default), price or name."),
    ] = "popularity",
    order: Annotated[
        Literal["asc", "desc"],
        Field(description="Sort order: asc or desc (default desc)."),
    ] = "desc",
    exclude_accounts: Annotated[
        bool,
        Field(
            description=(
                "If true, drop products classified as 'account' (heuristic) "
                "from the current page."
            )
        ),
    ] = False,
    platform: Annotated[
        str | None,
        Field(
            max_length=40,
            description=(
                "Optional client-side platform filter, e.g. 'steam', 'ps5', "
                "'xbox'. Filters the current page only."
            ),
        ),
    ] = None,
) -> dict:
    return _search_impl(
        name=name,
        page=page,
        per_page=per_page,
        currency=currency,
        sort=sort,
        order=order,
        exclude_accounts=exclude_accounts,
        platform=platform,
    )


if __name__ == "__main__":
    # Streamable HTTP server (Horizon/Prefect proxy fronts this).
    mcp.run(transport="http", host=SERVER_HOST, port=SERVER_PORT)
