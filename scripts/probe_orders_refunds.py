"""Second probe: verify dates_are_gmt behaviour and inspect refund payloads.

Read-only. Completes the two questions left open by probe_orders_window.py:
  1. Does `dates_are_gmt` actually change how bounds are interpreted?
  2. What shape do refunds have, and how should they be attributed to a window?

Also reports catalogue freshness, which decides whether a top-up generator
is needed before the demo recording.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import urllib3
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

ALL_STATUSES = "pending,processing,on-hold,completed,cancelled,refunded,failed"
UTC = ZoneInfo("UTC")
TIMEOUT = 30
MAX_PAGES = 20
LOOKBACK_DAYS = 90

_verify_tls = True


def env(name: str) -> str:
    """Return a required environment variable or exit."""
    value = os.getenv(name, "").strip()
    if not value:
        print(f"[FATAL] {name} is missing or empty in {ENV_PATH}")
        sys.exit(1)
    return value


def get(
    session: requests.Session, url: str, params: dict[str, Any]
) -> requests.Response:
    """GET with a one-time fallback to verify=False for self-signed certs."""
    global _verify_tls
    try:
        return session.get(url, params=params, timeout=TIMEOUT, verify=_verify_tls)
    except requests.exceptions.SSLError:
        if not _verify_tls:
            raise
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _verify_tls = False
        return session.get(url, params=params, timeout=TIMEOUT, verify=False)


def utc_str(dt: datetime) -> str:
    """Format an aware datetime as a naive UTC ISO string for the WC REST API."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def parse_gmt(value: str) -> datetime:
    """Parse a WooCommerce GMT timestamp into an aware UTC datetime."""
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def count_orders(session: requests.Session, url: str, params: dict[str, Any]) -> int:
    """Return X-WP-Total without downloading the orders themselves."""
    merged: dict[str, Any] = {"per_page": 1, "status": ALL_STATUSES, **params}
    response = get(session, url, merged)
    response.raise_for_status()
    return int(response.headers.get("X-WP-Total", "-1"))


def fetch_all(
    session: requests.Session, url: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """Fetch every order matching params, following pagination."""
    orders: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        merged: dict[str, Any] = {
            "per_page": 100,
            "page": page,
            "status": ALL_STATUSES,
            **params,
        }
        response = get(session, url, merged)
        response.raise_for_status()
        orders.extend(response.json())
        if page >= int(response.headers.get("X-WP-TotalPages", "1")):
            break
    return orders


def probe_freshness(
    session: requests.Session, url: str, tz: ZoneInfo
) -> list[dict[str, Any]]:
    """Report the age of the newest order and the daily distribution."""
    print("=" * 62)
    print("PROBE A - data freshness")
    print("=" * 62)
    now_local = datetime.now(tz)
    day_start = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    window_start = day_start - timedelta(days=LOOKBACK_DAYS - 1)
    orders = fetch_all(
        session,
        url,
        {
            "dates_are_gmt": "true",
            "after": utc_str(window_start - timedelta(seconds=1)),
            "orderby": "date",
            "order": "asc",
        },
    )
    print(f"lookback      : {LOOKBACK_DAYS} days")
    print(f"fetched       : {len(orders)} orders")
    if not orders:
        print("[FATAL] no orders in lookback window")
        sys.exit(1)

    per_day: Counter[str] = Counter()
    for order in orders:
        local_day = parse_gmt(order["date_created_gmt"]).astimezone(tz).date()
        per_day[local_day.isoformat()] += 1

    oldest = parse_gmt(orders[0]["date_created_gmt"]).astimezone(tz)
    newest = parse_gmt(orders[-1]["date_created_gmt"]).astimezone(tz)
    gap_days = (now_local.date() - newest.date()).days
    print(f"oldest local  : {oldest.isoformat()}")
    print(f"newest local  : {newest.isoformat()}")
    print(f"newest is     : {gap_days} day(s) before today")
    print()
    print("last 10 local days with orders:")
    for day in sorted(per_day)[-10:]:
        print(f"  {day}  {per_day[day]:>3}")
    return orders


def probe_gmt_flag(session: requests.Session, url: str, anchor_gmt: str) -> None:
    """Compare the two interpretations of an identical bound string."""
    print()
    print("=" * 62)
    print("PROBE B - does dates_are_gmt change interpretation?")
    print("=" * 62)
    bound = utc_str(parse_gmt(anchor_gmt) - timedelta(seconds=1))
    as_gmt = count_orders(session, url, {"after": bound, "dates_are_gmt": "true"})
    as_site = count_orders(session, url, {"after": bound, "dates_are_gmt": "false"})
    print(f"bound string        : {bound}")
    print(f"read as UTC   (true): {as_gmt} orders")
    print(f"read as site (false): {as_site} orders")
    if as_gmt == as_site:
        print("[WARN] identical - the flag may be ignored, or no orders")
        print("[WARN] fall inside the offset gap. Treat with suspicion.")
    else:
        print("OK - the same string is interpreted differently. Flag works.")


def probe_status_mix(orders: list[dict[str, Any]]) -> None:
    """Print the status breakdown across the whole lookback window."""
    print()
    print("=" * 62)
    print("PROBE C - status mix across lookback")
    print("=" * 62)
    tally: dict[str, list[float]] = {}
    for order in orders:
        tally.setdefault(str(order["status"]), []).append(float(order["total"]))
    print(f"{'status':<12}{'count':>7}{'sum(total)':>14}")
    print("-" * 33)
    for status in sorted(tally):
        values = tally[status]
        print(f"{status:<12}{len(values):>7}{sum(values):>14.2f}")


def probe_refunds(
    session: requests.Session,
    store_url: str,
    orders: list[dict[str, Any]],
    tz: ZoneInfo,
) -> None:
    """Inspect the refunds array and the dedicated refunds sub-resource."""
    print()
    print("=" * 62)
    print("PROBE D - refund shape and attribution")
    print("=" * 62)
    with_refunds = [o for o in orders if o.get("refunds")]
    refunded_status = [o for o in orders if o["status"] == "refunded"]
    print(f"orders with refunds[] : {len(with_refunds)}")
    print(f"orders status=refunded: {len(refunded_status)}")
    partial = [o for o in with_refunds if o["status"] != "refunded"]
    print(f"partial (refund but not status=refunded): {len(partial)}")
    if not with_refunds:
        print("[FATAL] no refunds found - widen LOOKBACK_DAYS")
        sys.exit(1)

    example = with_refunds[0]
    print()
    print(f"example order id   : {example['id']}")
    print(f"example status     : {example['status']}")
    print(f"order total        : {example['total']}")
    print(f"refunds[] payload  : {example['refunds']}")
    print(f"refunds[0] keys    : {sorted(example['refunds'][0].keys())}")

    detail_url = f"{store_url}/wp-json/wc/v3/orders/{example['id']}/refunds"
    response = get(session, detail_url, {"per_page": 100})
    response.raise_for_status()
    details: list[dict[str, Any]] = response.json()
    print()
    print(f"GET /orders/{example['id']}/refunds -> {len(details)} object(s)")
    if details:
        first = details[0]
        print(f"refund object keys : {sorted(first.keys())}")
        print(f"refund amount      : {first.get('amount')}")
        print(f"refund date_gmt    : {first.get('date_created_gmt')}")
        print(f"refund reason      : {first.get('reason')!r}")
        items: list[dict[str, Any]] = first.get("line_items", [])
        print(f"line_items count   : {len(items)}")
        if items:
            print(f"line_item keys     : {sorted(items[0].keys())}")
            print(f"line_item total    : {items[0].get('total')}")

    print()
    print("attribution drift - refund date vs parent order date:")
    print(f"{'order':>7}{'order day':>13}{'refund day':>13}{'drift':>7}")
    print("-" * 40)
    drifted = 0
    for order in with_refunds[:10]:
        order_day = parse_gmt(order["date_created_gmt"]).astimezone(tz).date()
        sub = get(
            session,
            f"{store_url}/wp-json/wc/v3/orders/{order['id']}/refunds",
            {"per_page": 100},
        )
        sub.raise_for_status()
        for refund in sub.json():
            raw = refund.get("date_created_gmt")
            if not raw:
                continue
            refund_day = parse_gmt(raw).astimezone(tz).date()
            delta = (refund_day - order_day).days
            drifted += 1 if delta else 0
            print(
                f"{order['id']:>7}{order_day.isoformat():>13}"
                f"{refund_day.isoformat():>13}{delta:>7}"
            )
    print()
    print(f"refunds landing on a different day than their order: {drifted}")


def main() -> None:
    load_dotenv(ENV_PATH)
    store_url = env("WOO_STORE_URL").rstrip("/")
    tz = ZoneInfo(env("BUSINESS_TIMEZONE"))
    orders_url = f"{store_url}/wp-json/wc/v3/orders"

    session = requests.Session()
    session.auth = (env("WOO_CONSUMER_KEY"), env("WOO_CONSUMER_SECRET"))

    orders = probe_freshness(session, orders_url, tz)
    probe_gmt_flag(session, orders_url, orders[-1]["date_created_gmt"])
    probe_status_mix(orders)
    probe_refunds(session, store_url, orders, tz)

    print()
    print(f"tls verification used: {_verify_tls}")


if __name__ == "__main__":
    main()
