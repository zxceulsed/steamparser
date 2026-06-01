#!/usr/bin/env python3
"""Parse the new Steam Community Market SSR listing data."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_LIMIT = 20
DEFAULT_EXPECT_CURRENCY_ID = 5


class ParserError(RuntimeError):
    """Raised when Steam data is missing or not in the expected shape."""


def fetch_html(url: str, cookie: str | None) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    }
    if cookie:
        headers["Cookie"] = cookie

    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise ParserError(f"Steam returned HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise ParserError(f"Could not fetch Steam page: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ParserError("Timed out while fetching Steam page") from exc


def cookie_json_to_header(cookie_data: Any) -> str:
    if isinstance(cookie_data, dict):
        cookie_data = cookie_data.get("cookies", cookie_data.get("data"))
    if not isinstance(cookie_data, list):
        raise ParserError("Cookie JSON must be a list or an object with a cookies list")

    parts: list[str] = []
    for cookie in cookie_data:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if isinstance(name, str) and isinstance(value, str) and name and value:
            parts.append(f"{name}={value}")

    if not parts:
        raise ParserError("Cookie JSON did not contain any name/value cookies")
    return "; ".join(parts)


def load_cookie_header_from_json(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as file:
            cookie_data = json.load(file)
    except OSError as exc:
        raise ParserError(f"Could not read cookie JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ParserError(f"Could not decode cookie JSON file: {path}") from exc
    return cookie_json_to_header(cookie_data)


def parse_render_context(html: str) -> dict[str, Any]:
    match = re.search(
        r'window\.SSR\.renderContext\s*=\s*JSON\.parse\("((?:\\.|[^"\\])*)"\)',
        html,
        re.DOTALL,
    )
    if not match:
        raise ParserError("Could not find window.SSR.renderContext JSON in Steam HTML")

    try:
        render_context_json = json.loads(f'"{match.group(1)}"')
        render_context = json.loads(render_context_json)
    except json.JSONDecodeError as exc:
        raise ParserError("Could not decode window.SSR.renderContext JSON") from exc

    if not isinstance(render_context, dict):
        raise ParserError("window.SSR.renderContext did not decode to an object")
    return render_context


def parse_query_data(render_context: dict[str, Any]) -> dict[str, Any]:
    query_data_raw = render_context.get("queryData")
    if not isinstance(query_data_raw, str):
        raise ParserError("window.SSR.renderContext.queryData is missing")

    try:
        query_data = json.loads(query_data_raw)
    except json.JSONDecodeError as exc:
        raise ParserError("Could not decode window.SSR.renderContext.queryData") from exc

    if not isinstance(query_data, dict):
        raise ParserError("queryData did not decode to an object")
    return query_data


def query_key(query: dict[str, Any]) -> list[Any]:
    key = query.get("queryKey")
    return key if isinstance(key, list) else []


def query_state_data(query: dict[str, Any]) -> Any:
    state = query.get("state")
    if not isinstance(state, dict):
        return None
    return state.get("data")


def find_market_item_search(queries: list[Any]) -> dict[str, Any]:
    for query in queries:
        if not isinstance(query, dict):
            continue
        key = query_key(query)
        if key and key[0] == "market_item_search":
            data = query_state_data(query)
            if isinstance(data, dict) and get_first_page_listings(data, strict=False) is not None:
                return data
    raise ParserError("Could not find market_item_search data in queryData")


def find_orderbook(queries: list[Any]) -> dict[str, Any]:
    for query in queries:
        if not isinstance(query, dict):
            continue
        key = query_key(query)
        if len(key) >= 2 and key[0] == "market" and key[1] == "orderbook":
            data = query_state_data(query)
            if isinstance(data, dict):
                return data
    raise ParserError("Could not find market/orderbook data in queryData")


def as_money(units: Any) -> float:
    if not isinstance(units, int):
        raise ParserError(f"Expected integer money amount, got {units!r}")
    return float(Decimal(units) / Decimal(100))


def get_property(asset: dict[str, Any], property_id: int, value_key: str) -> Any:
    properties = asset.get("asset_properties")
    if not isinstance(properties, list):
        return None

    for prop in properties:
        if not isinstance(prop, dict):
            continue
        if prop.get("propertyid") == property_id:
            return prop.get(value_key)
    return None


def get_listing_asset(listing: dict[str, Any]) -> dict[str, Any]:
    asset = listing.get("asset")
    if isinstance(asset, dict):
        return asset
    raise ParserError(f"Listing {listing.get('listingid', '<unknown>')} has no asset data")


def get_first_page_listings(item_search: dict[str, Any], strict: bool = True) -> list[Any] | None:
    direct_listings = item_search.get("listings")
    if isinstance(direct_listings, list):
        return direct_listings

    pages = item_search.get("pages")
    if not isinstance(pages, list) or not pages:
        if not strict:
            return None
        raise ParserError("market_item_search has no pages")

    for page in pages:
        if not isinstance(page, dict):
            continue

        listings = page.get("listings")
        if isinstance(listings, list):
            return listings

    if not strict:
        return None
    raise ParserError("market_item_search pages have no listings")


def get_item_name(listings: list[Any], orderbook_query_key_name: str | None = None) -> str:
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        asset = listing.get("asset")
        if not isinstance(asset, dict):
            continue
        description = asset.get("description")
        if not isinstance(description, dict):
            continue
        market_hash_name = description.get("market_hash_name")
        if isinstance(market_hash_name, str) and market_hash_name:
            return market_hash_name

    if orderbook_query_key_name:
        return orderbook_query_key_name
    raise ParserError("Could not determine item name from listings")


def get_orderbook_item_name(queries: list[Any]) -> str | None:
    for query in queries:
        if not isinstance(query, dict):
            continue
        key = query_key(query)
        if len(key) >= 4 and key[0] == "market" and key[1] == "orderbook":
            return key[3] if isinstance(key[3], str) else None
    return None


def parse_market_page(html: str, limit: int, expected_currency_id: int | None) -> dict[str, Any]:
    render_context = parse_render_context(html)
    query_data = parse_query_data(render_context)
    queries = query_data.get("queries")
    if not isinstance(queries, list):
        raise ParserError("queryData.queries is missing")

    item_search = find_market_item_search(queries)
    orderbook = find_orderbook(queries)
    currency_id = orderbook.get("eCurrency")

    if expected_currency_id is not None and currency_id != expected_currency_id:
        raise ParserError(
            f"Steam returned currency_id={currency_id}, expected {expected_currency_id}. "
            "Pass valid Steam cookies for the expected account currency or override "
            "--expect-currency-id."
        )

    first_page_listings = get_first_page_listings(item_search)
    if first_page_listings is None:
        raise ParserError("market_item_search pages have no listings")
    raw_listings = first_page_listings[:limit]
    listings: list[dict[str, Any]] = []

    for raw_listing in raw_listings:
        if not isinstance(raw_listing, dict):
            continue

        asset = get_listing_asset(raw_listing)
        un_price = raw_listing.get("unPrice")
        un_fee = raw_listing.get("unFee")
        if not isinstance(un_price, int) or not isinstance(un_fee, int):
            raise ParserError(
                f"Listing {raw_listing.get('listingid', '<unknown>')} has invalid price data"
            )

        pattern = get_property(asset, 1, "int_value")
        wear_float = get_property(asset, 2, "float_value")

        listings.append(
            {
                "listing_id": raw_listing.get("listingid"),
                "price": as_money(un_price + un_fee),
                "price_text": raw_listing.get("strSubtotal"),
                "pattern": int(pattern) if isinstance(pattern, str) and pattern.isdigit() else pattern,
                "float": wear_float,
            }
        )

    return {
        "item_name": get_item_name(raw_listings, get_orderbook_item_name(queries)),
        "currency_id": currency_id,
        "autobuy_price": as_money(orderbook.get("amtMaxBuyOrder")),
        "autobuy_orders_count": orderbook.get("cBuyOrders"),
        "sell_orders_count": orderbook.get("cSellOrders"),
        "listings": listings,
    }


def fetch_market_page(
    url: str,
    cookie: str | None,
    limit: int,
    expected_currency_id: int | None,
    attempts: int = 3,
    retry_delay_seconds: float = 1.0,
) -> dict[str, Any]:
    last_result: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        html = fetch_html(url, cookie)
        result = parse_market_page(html, limit, expected_currency_id)
        last_result = result
        if result.get("listings") or not result.get("sell_orders_count"):
            return result
        if attempt < attempts:
            time.sleep(retry_delay_seconds)

    if last_result is None:
        raise ParserError("Steam page was not fetched")
    return last_result


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def optional_currency_id(value: str) -> int | None:
    if value.lower() in {"none", "any"}:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer, 'none', or 'any'") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse Steam Community Market SSR listings into JSON."
    )
    parser.add_argument("--url", required=True, help="Steam Community Market listing URL")
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=DEFAULT_LIMIT,
        help=f"number of first-page listings to output, default: {DEFAULT_LIMIT}",
    )
    parser.add_argument(
        "--cookie",
        default=os.environ.get("STEAM_COOKIE"),
        help="Steam Cookie header value; defaults to STEAM_COOKIE env var",
    )
    parser.add_argument(
        "--cookie-json",
        help="browser-exported cookies.json file; ignored when --cookie is set",
    )
    parser.add_argument(
        "--expect-currency-id",
        type=optional_currency_id,
        default=DEFAULT_EXPECT_CURRENCY_ID,
        help=(
            "expected Steam currency id; use 'any' to disable the check, "
            f"default: {DEFAULT_EXPECT_CURRENCY_ID}"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cookie = args.cookie
        if not cookie and args.cookie_json:
            cookie = load_cookie_header_from_json(args.cookie_json)
        result = fetch_market_page(args.url, cookie, args.limit, args.expect_currency_id)
    except ParserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
