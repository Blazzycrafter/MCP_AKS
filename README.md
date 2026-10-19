# AKS — AllKeyShop MCP server

A small [FastMCP](https://github.com/jlowin/fastmcp) server that exposes one
tool, `search`, for comparing game-key prices across resellers (Driffle, Eneba,
G2A, Kinguin, GAMIVO, PS Store, Nintendo eShop, …) via the public AllKeyShop
catalogue API.

The tool queries the upstream `CatalogV2` action in `price_card` mode, so each
product carries the **single best (cheapest) offer** for the requested currency;
`offers_count` is the total number of offers across all merchants.

## Tool: `search`

| Parameter          | Type                          | Default      | Description |
|--------------------|-------------------------------|--------------|-------------|
| `name`             | string, 1–80 chars (required) | —            | Game name, e.g. `minecraft`, `elden ring`, `Warhammer 40,000`. |
| `page`             | int ≥ 1                       | `1`          | 1-based result page (maps to upstream `pagenum`). |
| `per_page`         | int 1–50                      | `20`         | Results per page (maps to upstream `per_page`). |
| `currency`         | `eur` \| `usd` \| `gbp`       | `eur`        | Price currency (the only currencies the upstream accepts). |
| `sort`             | `popularity` \| `price` \| `name` | `popularity` | Sort field (`popularity` → upstream `popularity_score`). |
| `order`            | `asc` \| `desc`               | `desc`       | Sort order. |
| `exclude_accounts` | bool                          | `false`      | Drop products classified as `account` (heuristic, current page only). |
| `platform`         | string ≤ 40 (optional)        | `null`       | Client-side platform filter, e.g. `steam`, `ps5`, `xbox` (current page only). |

### Response

```jsonc
{
  "status": "success",
  "query": "god of war",
  "currency": "eur",
  "count": 5,
  "pagination": { "total": 38, "per_page": 20, "pagenum": 1, "total_pages": 2 },
  "warnings": [],
  "products": [
    {
      "id": 1,
      "name": "God of War",
      "type": "game",
      "link": "https://www.allkeyshop.com/blog/compare-and-buy-cd-key-for-digital-download-god-of-war/",
      "offers_count": 200,
      "activation_platforms": ["Steam"],
      "best_offer": {
        "merchant": "Eneba",
        "merchant_id": 5,
        "price": 9.99,
        "currency": "eur",
        "buy_url": "https://www.allkeyshop.com/redirection/offer/eur/…",
        "edition": "Unknown",
        "region": "GLOBAL",
        "region_display": "GLOBAL",
        "stock_status": "in_stock",
        "voucher": { "code": "AKSMAX", "discount": 0.17 },
        "is_free": false
      },
      "official_price": { "merchant": "eTailcard", "price": 24.5 },
      "historical_low": { "merchant": "Kinguin", "price": 5.39, "date": "2024-10-15" },
      "listing_kind": "key"
    }
  ]
}
```

`products` is an **ordered list** in upstream order. On failure the envelope is
one of `invalid_input`, `rate_limited`, `upstream_error` (network/timeout/
non-JSON), or the upstream status (e.g. `failed`). Raw upstream error text is
**not** forwarded — only a generic `message` — while upstream `warnings` are
passed through.

### Heuristics and caveats

* **`listing_kind`** (`key`/`account`/`gift`) uses the upstream product
  `type` field when it carries a recognised value (`account`/`key`/`gift`).
  When the type is missing or unknown (e.g. `game`, `dlc`), it falls back to a
  heuristic: `account` if the word `account` appears anywhere in the product
  name (so `"… Account Deluxe Edition"` and `"… Account PS5"` are caught, not
  only a trailing `" Account"`), `gift` if the best offer's region name
  contains `"Gift"`, otherwise `key`. `exclude_accounts` filters on this
  classification.
* **`platform` is client-side and only filters the current page.** No
  server-side platform filter is known, so cross-page platform filtering is not
  possible with a single request. Matching is case-insensitive and looks at the
  activation-platform names and at the product name (suffix / whole word).
* **`is_free`** marks offers with a price of `0` (e.g. free-to-keep DLC on a
  store). Such a `best_offer` is not a paid bargain and should be presented
  accordingly.
* Only the **best offer** per product is returned; use `offers_count` to see how
  many offers exist in total.
* The upstream can occasionally return HTML (e.g. `503 Backend fetch failed`) or
  a `failed` status with an ElasticSearch timeout; both are surfaced as a clean
  error envelope rather than a crash.

## Running locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python main.py            # serves Streamable HTTP on 0.0.0.0:8000
```

Environment variables (all optional):

| Variable               | Default | Meaning |
|------------------------|---------|---------|
| `AKS_UPSTREAM_URL`     | AllKeyShop `vaks.php` URL | Upstream endpoint. |
| `AKS_REQUEST_TIMEOUT`  | `20`    | Upstream request timeout (seconds). |
| `AKS_RATE_LIMIT_MAX`   | `10`    | Max calls per window (per process). |
| `AKS_RATE_LIMIT_WINDOW`| `10.0`  | Rate-limit window (seconds). |
| `AKS_HOST`             | `0.0.0.0` | HTTP bind host. |
| `AKS_PORT`             | `8000`  | HTTP bind port. |

## Tests

```bash
pytest -q                 # offline unit tests (requests.get is mocked)
python test_local.py      # live smoke test against localhost:8000
```

The offline suite (`tests/test_search.py`) covers success, pagination/currency/
sort mapping, the account/platform heuristics, free offers, empty results,
upstream `failed`, HTML 503, non-JSON, timeouts, broken product entries and input
validation (including titles such as `Warhammer 40,000`).

## Known deployment drift

The FastMCP Cloud deployment has been observed **not** running this hardened
code: an empty `name` was accepted, quotes/`<` passed through, the response had
no `status` field, and the tool schema had no description. This repository
(`main`) is the hardened version; the deployed instance lags behind. The deploy
source/branch for FastMCP Cloud is currently unknown (see PR notes).
