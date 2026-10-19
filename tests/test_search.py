"""Offline unit tests for the AKS MCP server search tool.

These tests never hit the network: ``requests.get`` is monkeypatched. They cover
success paths, pagination/currency/sort mapping, the account and platform
heuristics, free (price 0) offers, and a range of upstream failure modes.

Run:  pytest -q
"""

from __future__ import annotations

import pytest
import requests
from pydantic import TypeAdapter, ValidationError

import main


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload=None, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.reset_rate_limiter()
    yield
    main.reset_rate_limiter()


def make_get(monkeypatch, payload=None, *, text="", status=200, exc=None, capture=None):
    def fake_get(url, params=None, timeout=None):
        if capture is not None:
            capture["url"] = url
            capture["params"] = params
            capture["timeout"] = timeout
        if exc is not None:
            raise exc
        return FakeResponse(payload=payload, text=text, status_code=status)

    monkeypatch.setattr(main.requests, "get", fake_get)


def make_offer(
    price,
    merchant="Kinguin",
    merchant_id=47,
    region="Windows 10 EU",
    region_display="EUROPE",
    stock="in_stock",
    buy_url="https://buy.example/1",
    edition="Unknown",
    voucher=None,
):
    offer = {
        "id": 1,
        "price": price,
        "merchant": {"id": merchant_id, "type": 0, "name": merchant},
        "region": {"name": region, "display_name": region_display},
        "stock_status": stock,
        "buy_url": buy_url,
        "edition": {"name": edition},
    }
    if voucher is not None:
        offer["voucher"] = voucher
    return offer


def make_product(
    pid,
    name,
    *,
    offers_count=10,
    offer=None,
    official=None,
    low=None,
    platforms=None,
    type_="game",
    link=None,
):
    product = {
        "id": pid,
        "name": name,
        "type": type_,
        "link": link or f"https://www.allkeyshop.com/blog/{pid}/",
        "offers_count": offers_count,
        "offers": [offer] if offer is not None else [],
    }
    if platforms is not None:
        product["activation_platforms"] = [{"name": p} for p in platforms]
    if official is not None:
        product["official_offers"] = official
    if low is not None:
        product["historical_low_offers"] = low
    return product


def success_payload(products, *, pagenum=1, per_page=20, total=None, total_pages=None, warnings=None):
    return {
        "status": "success",
        "warnings": warnings or [],
        "errors": [],
        "pagination": {
            "total": len(products) if total is None else total,
            "per_page": per_page,
            "pagenum": pagenum,
            "total_pages": 1 if total_pages is None else total_pages,
        },
        "products": products,
    }


# --------------------------------------------------------------------------
# Basic success / output shape
# --------------------------------------------------------------------------
def test_success_basic_shape(monkeypatch):
    payload = success_payload(
        [
            make_product(
                216,
                "Minecraft",
                offers_count=94,
                platforms=["Steam", "Microsoft Windows"],
                offer=make_offer(13.52, buy_url="https://buy/mc"),
                official=[{"merchant": {"name": "eTailcard"}, "price": 24.5}],
                low=[{"merchant": {"name": "Kinguin"}, "price": 5.39, "date": "2024-10-15"}],
            )
        ]
    )
    make_get(monkeypatch, payload)

    result = main._search_impl("minecraft")

    assert result["status"] == "success"
    assert result["query"] == "minecraft"
    assert result["currency"] == "eur"
    assert result["count"] == 1
    assert isinstance(result["products"], list)

    product = result["products"][0]
    assert product["id"] == 216
    assert product["name"] == "Minecraft"
    assert product["type"] == "game"
    assert product["link"] == "https://www.allkeyshop.com/blog/216/"
    assert product["offers_count"] == 94
    assert product["activation_platforms"] == ["Steam", "Microsoft Windows"]
    assert product["listing_kind"] == "key"

    best = product["best_offer"]
    assert best["merchant"] == "Kinguin"
    assert best["merchant_id"] == 47
    assert best["price"] == 13.52
    assert best["currency"] == "eur"
    assert best["buy_url"] == "https://buy/mc"
    assert best["region"] == "Windows 10 EU"
    assert best["region_display"] == "EUROPE"
    assert best["stock_status"] == "in_stock"
    assert best["is_free"] is False

    assert product["official_price"] == {"merchant": "eTailcard", "price": 24.5}
    assert product["historical_low"] == {
        "merchant": "Kinguin",
        "price": 5.39,
        "date": "2024-10-15",
    }


def test_products_keep_upstream_order(monkeypatch):
    payload = success_payload(
        [
            make_product(3, "Third", offer=make_offer(1)),
            make_product(1, "First", offer=make_offer(2)),
            make_product(2, "Second", offer=make_offer(3)),
        ]
    )
    make_get(monkeypatch, payload)

    result = main._search_impl("order")
    assert [p["name"] for p in result["products"]] == ["Third", "First", "Second"]


def test_best_offer_picks_cheapest(monkeypatch):
    product = make_product(1, "Multi", offer=make_offer(9.99))
    product["offers"] = [make_offer(9.99), make_offer(4.5), make_offer(7.0)]
    make_get(monkeypatch, success_payload([product]))

    best = main._search_impl("multi")["products"][0]["best_offer"]
    assert best["price"] == 4.5


def test_voucher_and_free_flag(monkeypatch):
    product = make_product(
        1,
        "Free DLC",
        offer=make_offer(0, voucher={"code": "AKSMAX", "discount": 0.17}),
    )
    make_get(monkeypatch, success_payload([product]))

    best = main._search_impl("free dlc")["products"][0]["best_offer"]
    assert best["price"] == 0
    assert best["is_free"] is True
    assert best["voucher"] == {"code": "AKSMAX", "discount": 0.17}


# --------------------------------------------------------------------------
# Parameter mapping
# --------------------------------------------------------------------------
def test_pagination_and_sort_mapping(monkeypatch):
    capture = {}
    make_get(monkeypatch, success_payload([]), capture=capture)

    main._search_impl(
        "minecraft",
        page=3,
        per_page=50,
        currency="usd",
        sort="price",
        order="asc",
    )

    params = capture["params"]
    assert params["action"] == "CatalogV2"
    assert params["search_name"] == "minecraft"
    assert params["pagenum"] == 3
    assert params["per_page"] == 50
    assert params["currency"] == "usd"
    assert params["sort_field"] == "price"
    assert params["sort_order"] == "asc"
    assert params["price_mode"] == "price_card"
    assert "offers.price" in params["fields"]


@pytest.mark.parametrize(
    "sort,expected",
    [("popularity", "popularity_score"), ("price", "price"), ("name", "name")],
)
def test_sort_field_mapping(monkeypatch, sort, expected):
    capture = {}
    make_get(monkeypatch, success_payload([]), capture=capture)
    main._search_impl("x", sort=sort)
    assert capture["params"]["sort_field"] == expected


def test_currency_propagated_to_offers(monkeypatch):
    product = make_product(1, "Game", offer=make_offer(10))
    make_get(monkeypatch, success_payload([product]))

    result = main._search_impl("game", currency="gbp")
    assert result["currency"] == "gbp"
    assert result["products"][0]["best_offer"]["currency"] == "gbp"


# --------------------------------------------------------------------------
# Heuristics: listing_kind / exclude_accounts / platform
# --------------------------------------------------------------------------
def test_listing_kind_account_gift_key(monkeypatch):
    products = [
        make_product(1, "Some Game Account", offer=make_offer(5)),
        make_product(2, "Some Game", offer=make_offer(5, region="Gift")),
        make_product(3, "Some Game", offer=make_offer(5, region="GLOBAL")),
    ]
    make_get(monkeypatch, success_payload(products))

    kinds = [p["listing_kind"] for p in main._search_impl("some")["products"]]
    assert kinds == ["account", "gift", "key"]


def test_listing_kind_uses_upstream_type_with_suffix(monkeypatch):
    # Codex P2: type="account" but the name has text after "Account"
    # (edition/platform). Must still classify as account and be filtered.
    products = [
        make_product(1, "God of War Ragnarok Account Deluxe Edition",
                     type_="account", offer=make_offer(19.99)),
        make_product(2, "God of War Ragnarok Account PS5",
                     type_="account", offer=make_offer(19.99)),
        make_product(3, "God of War", type_="game", offer=make_offer(9.99)),
    ]
    make_get(monkeypatch, success_payload(products))

    result = main._search_impl("god of war")
    kinds = {p["id"]: p["listing_kind"] for p in result["products"]}
    assert kinds == {1: "account", 2: "account", 3: "key"}

    filtered = main._search_impl("god of war", exclude_accounts=True)
    assert [p["id"] for p in filtered["products"]] == [3]


def test_listing_kind_fallback_when_type_missing_or_unknown(monkeypatch):
    products = [
        # no type field at all -> name heuristic catches "Account" anywhere
        {"id": 1, "name": "Some Game Account PS5", "offers": [make_offer(5)]},
        # unknown type -> name heuristic
        make_product(2, "Some Game Account Deluxe Edition", type_="mystery",
                     offer=make_offer(5)),
        # unknown type, no account word -> key
        make_product(3, "Some Game", type_="mystery", offer=make_offer(5)),
        # missing type, no account word -> key
        {"id": 4, "name": "Some Game", "offers": [make_offer(5)]},
    ]
    make_get(monkeypatch, success_payload(products))

    result = main._search_impl("some")
    kinds = {p["id"]: p["listing_kind"] for p in result["products"]}
    assert kinds == {1: "account", 2: "account", 3: "key", 4: "key"}


def test_listing_kind_recognised_type_key_and_gift(monkeypatch):
    products = [
        make_product(1, "Some Game", type_="key", offer=make_offer(5)),
        make_product(2, "Some Game", type_="gift", offer=make_offer(5)),
    ]
    make_get(monkeypatch, success_payload(products))

    kinds = [p["listing_kind"] for p in main._search_impl("some")["products"]]
    assert kinds == ["key", "gift"]


def test_exclude_accounts(monkeypatch):
    products = [
        make_product(1, "God of War", offer=make_offer(9.99)),
        make_product(2, "God of War PS5 Account", offer=make_offer(19.99)),
        make_product(3, "God of War Steam Account", offer=make_offer(7.5)),
    ]
    make_get(monkeypatch, success_payload(products))

    result = main._search_impl("god of war", exclude_accounts=True)
    assert result["count"] == 1
    assert [p["id"] for p in result["products"]] == [1]


def test_platform_filter(monkeypatch):
    products = [
        make_product(1, "God of War", platforms=["Steam"], offer=make_offer(9.99)),
        make_product(2, "God of War Ragnarok PS5 Account", platforms=["PS5"], offer=make_offer(19.99)),
        make_product(3, "God of War Steam Account", platforms=["Steam"], offer=make_offer(7.5)),
        make_product(4, "God of War PS4 Account", platforms=["PS4"], offer=make_offer(5.0)),
    ]
    make_get(monkeypatch, success_payload(products))

    result = main._search_impl("god of war", platform="ps5")
    assert [p["id"] for p in result["products"]] == [2]

    result = main._search_impl("god of war", platform="STEAM")
    assert [p["id"] for p in result["products"]] == [1, 3]

    result = main._search_impl("god of war", platform="xbox")
    assert result["count"] == 0


def test_platform_short_token_is_not_a_substring(monkeypatch):
    # "ea" must not match "Steam" (regression guard for CodeRabbit finding).
    products = [
        make_product(1, "EA Sports FC", platforms=["EA App"], offer=make_offer(10)),
        make_product(2, "Some Steam Game", platforms=["Steam"], offer=make_offer(10)),
    ]
    make_get(monkeypatch, success_payload(products))
    result = main._search_impl("game", platform="ea")
    assert [p["id"] for p in result["products"]] == [1]


def test_platform_multiword_token(monkeypatch):
    products = [
        make_product(1, "Game Xbox One", platforms=["Xbox One"], offer=make_offer(10)),
        make_product(2, "Game Xbox Series", platforms=["Xbox Series"], offer=make_offer(10)),
    ]
    make_get(monkeypatch, success_payload(products))
    result = main._search_impl("game", platform="xbox one")
    assert [p["id"] for p in result["products"]] == [1]


# --------------------------------------------------------------------------
# Empty / failure modes
# --------------------------------------------------------------------------
def test_empty_results(monkeypatch):
    make_get(monkeypatch, success_payload([]))
    result = main._search_impl("nothing")
    assert result["status"] == "success"
    assert result["count"] == 0
    assert result["products"] == []


def test_upstream_failed_passthrough(monkeypatch):
    payload = {
        "status": "failed",
        "warnings": ["Invalid field 'bogus' has been removed from selection."],
        "errors": ["Internal error (Exception: ElasticSearch curl error (28))"],
        "products": [],
    }
    make_get(monkeypatch, payload)

    result = main._search_impl("minecraft")
    assert result["status"] == "failed"
    assert result["errors"] == ["Internal error (Exception: ElasticSearch curl error (28))"]
    assert result["warnings"] == ["Invalid field 'bogus' has been removed from selection."]
    assert result["products"] == []


def test_html_503(monkeypatch):
    make_get(monkeypatch, text="<html><body>Backend fetch failed</body></html>", status=503)
    result = main._search_impl("minecraft")
    assert result["status"] == "upstream_error"
    assert result["products"] == []


def test_non_json_200(monkeypatch):
    make_get(monkeypatch, payload=None, text="<html>not json</html>", status=200)
    result = main._search_impl("minecraft")
    assert result["status"] == "upstream_error"
    assert result["products"] == []


def test_timeout(monkeypatch):
    make_get(monkeypatch, exc=requests.exceptions.Timeout("timed out"))
    result = main._search_impl("minecraft")
    assert result["status"] == "upstream_error"
    assert result["detail"] == "Timeout"


def test_connection_error(monkeypatch):
    make_get(monkeypatch, exc=requests.exceptions.ConnectionError("boom"))
    result = main._search_impl("minecraft")
    assert result["status"] == "upstream_error"
    assert result["detail"] == "ConnectionError"


def test_top_level_not_dict(monkeypatch):
    make_get(monkeypatch, payload=["unexpected"])
    result = main._search_impl("minecraft")
    assert result["status"] == "upstream_error"


def test_broken_product_entries_do_not_crash(monkeypatch):
    products = [
        "not-a-dict",
        None,
        42,
        {"id": "x", "name": 123, "offers": "nope", "offers_count": "many", "activation_platforms": "x"},
        {"id": 7, "name": "Good", "offers": [{"price": "free", "merchant": "notadict"}, {"price": 5, "merchant": {"name": "M"}}], "offers_count": 2},
    ]
    make_get(monkeypatch, success_payload(products))

    result = main._search_impl("broken")
    assert result["status"] == "success"
    # Only the two dict entries survive; malformed types become null.
    assert result["count"] == 2
    broken = result["products"][0]
    assert broken["id"] is None
    assert broken["name"] is None
    assert broken["offers_count"] is None
    assert broken["best_offer"] is None
    assert result["products"][1]["best_offer"]["price"] == 5


def test_whitespace_only_is_invalid(monkeypatch):
    make_get(monkeypatch, success_payload([]))
    result = main._search_impl("   ")
    assert result["status"] == "invalid_input"
    assert result["products"] == []


def test_rate_limited(monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_MAX", 1)
    make_get(monkeypatch, success_payload([]))

    assert main._search_impl("one")["status"] == "success"
    assert main._search_impl("two")["status"] == "rate_limited"


# --------------------------------------------------------------------------
# Input validation (schema level)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "Warhammer 40,000",
        "Tom Clancy's Rainbow Six: Siege",
        "Half-Life 2",
        "C# Programming",
        "F1 24",
        "Marvel's Spider-Man",
        "Rocket League®",
        "Football Manager™ 2024",
        "S.T.A.L.K.E.R.",
    ],
)
def test_valid_names_accepted(value):
    adapter = TypeAdapter(main.SearchName)
    assert adapter.validate_python(value) == value


@pytest.mark.parametrize("value", ["drop; table", "a" * 81, "", "bad\nname", "semi;colon"])
def test_invalid_names_rejected(value):
    adapter = TypeAdapter(main.SearchName)
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


# --------------------------------------------------------------------------
# God of War live-observed fixture (Kai's findings, 2026-10-19)
# --------------------------------------------------------------------------
GOW_PAGE_1 = [
    make_product(1, "God of War", offers_count=200, platforms=["Steam"],
                 offer=make_offer(9.99, merchant="Eneba", merchant_id=5, buy_url="https://buy/gow", region="GLOBAL", region_display="GLOBAL")),
    make_product(2, "God of War Ragnarok PS5 Account", offers_count=38, platforms=["PS5"],
                 offer=make_offer(19.99, merchant="G2A", merchant_id=6, buy_url="https://buy/gowr-ps5", region="PS Store US", region_display="US")),
    make_product(3, "God of War Steam Account", offers_count=12, platforms=["Steam"],
                 offer=make_offer(7.5, merchant="Kinguin", merchant_id=47, buy_url="https://buy/gow-steam-acc", region="GLOBAL", region_display="GLOBAL")),
    make_product(4, "God of War PS4 Account", offers_count=20, platforms=["PS4"],
                 offer=make_offer(5.0, merchant="Driffle", merchant_id=9, buy_url="https://buy/gow-ps4-acc", region="PS Store UK", region_display="UK")),
    make_product(5, "God of War Ragnarok Valhalla PS5/PS4", offers_count=3, platforms=["PS5", "PS4"],
                 offer=make_offer(0, merchant="PS Store US", merchant_id=100, buy_url="https://buy/gowr-valhalla", region="PS Store US", region_display="US")),
]
GOW_PAGE_2 = [
    make_product(6, "God of War Ragnarok PS5", offers_count=50, platforms=["PS5"],
                 offer=make_offer(29.99, merchant="PS Store UK", merchant_id=101, buy_url="https://buy/gowr-ps5-key", region="PS Store UK", region_display="UK")),
    make_product(7, "God of War Collection PS3 Account", offers_count=8, platforms=["PS3"],
                 offer=make_offer(4.0, merchant="Eneba", merchant_id=5, buy_url="https://buy/gow-coll", region="GLOBAL", region_display="GLOBAL")),
]


def test_gow_page_1_and_pagination(monkeypatch):
    make_get(monkeypatch, success_payload(GOW_PAGE_1, pagenum=1, per_page=20, total=38, total_pages=2))
    result = main._search_impl("God of War", page=1, per_page=20)

    assert result["status"] == "success"
    assert result["count"] == 5
    assert result["pagination"]["total"] == 38
    assert result["pagination"]["total_pages"] == 2
    assert result["pagination"]["pagenum"] == 1
    # Shop link and buy_url are present for every product.
    assert all(p["link"] for p in result["products"])
    assert all(p["best_offer"]["buy_url"] for p in result["products"])


def test_gow_page_2(monkeypatch):
    capture = {}
    make_get(monkeypatch, success_payload(GOW_PAGE_2, pagenum=2, per_page=20, total=38, total_pages=2), capture=capture)
    result = main._search_impl("God of War", page=2, per_page=20)

    assert capture["params"]["pagenum"] == 2
    assert result["count"] == 2
    assert [p["id"] for p in result["products"]] == [6, 7]


def test_gow_exclude_accounts(monkeypatch):
    make_get(monkeypatch, success_payload(GOW_PAGE_1, total=38, total_pages=2))
    result = main._search_impl("God of War", exclude_accounts=True)

    ids = [p["id"] for p in result["products"]]
    assert ids == [1, 5]  # the key and the free DLC survive
    assert result["count"] == 2
    assert all(p["listing_kind"] != "account" for p in result["products"])


def test_gow_platform_filter_current_page(monkeypatch):
    make_get(monkeypatch, success_payload(GOW_PAGE_1, total=38, total_pages=2))
    result = main._search_impl("God of War", platform="ps5")

    ids = [p["id"] for p in result["products"]]
    assert ids == [2, 5]  # PS5 account + PS5/PS4 free DLC


def test_gow_free_dlc_flagged(monkeypatch):
    make_get(monkeypatch, success_payload(GOW_PAGE_1, total=38, total_pages=2))
    result = main._search_impl("God of War")

    free = [p for p in result["products"] if p["id"] == 5][0]
    assert free["best_offer"]["price"] == 0
    assert free["best_offer"]["is_free"] is True
    # Paid products must not be flagged free.
    paid = [p for p in result["products"] if p["id"] == 1][0]
    assert paid["best_offer"]["is_free"] is False
