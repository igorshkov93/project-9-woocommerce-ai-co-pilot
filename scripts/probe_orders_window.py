"""Probe the WooCommerce orders endpoint before implementing get_orders_summary.

Read-only. Answers four questions that the summary tool depends on:
  1. Do the REST credentials work over the LocalWP TLS certificate?
  2. Does `dates_are_gmt=true` really filter on date_created_gmt?
  3. Are the `after` / `before` bounds exclusive?
  4. What does the `refunds` array on an order actually contain?
"""

from __future__ import annotations

import os
import sys
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
TIMEOUT = 30
MAX_PAGES = 20

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
        print("[WARN] TLS verification failed. Retrying with verify=False")
        print("[WARN] (expected for a LocalWP self-signed certificate).")
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _verify_tls = False
        return session.get(url, params=params, timeout=TIMEOUT, verify=False)


def utc_str(dt: datetime) -> str:
    """Format an aware datetime as a naive UTC ISO string for the WC REST API."""
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S")


def count_orders(
    session: requests.Session,
    url: str,
    after: str | None,
    before: str | None,
    dates_are_gmt: bool,
) -> int:
    """Return X-WP-Total for a window without downloading the orders."""
    params: dict[str, Any] = {
        "per_page": 1,
        "status": ALL_STATUSES,
        "dates_are_gmt": "true" if dates_are_gmt else "false",
    }
    if after is not None:
        params["after"] = after
    if before is not None:
        params["before"] = before
    response = get(session, url, params)
    response.raise_for_status()
    return int(response.headers.get("X-WP-Total", "-1"))


def fetch_all(
    session: requests.Session, url: str, after: str, before: str
) -> list[dict[str, Any]]:
    """Fetch every order in a GMT window, following pagination."""
    orders: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        params: dict[str, Any] = {
            "per_page": 100,
            "page": page,
            "status": ALL_STATUSES,
            "dates_are_gmt": "true",
            "after": after,
            "before": before,
            "orderby": "date",
            "order": "asc",
        }
        response = get(session, url, params)
        response.raise_for_status()
        batch: list[dict[str, Any]] = response.json()
        orders.extend(batch)
        total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
    return orders


def main() -> None:
    load_dotenv(ENV_PATH)
    store_url = env("WOO_STORE_URL").rstrip("/")
    tz_name = env("BUSINESS_TIMEZONE")
    tz = ZoneInfo(tz_name)
    orders_url = f"{store_url}/wp-json/wc/v3/orders"

    session = requests.Session()
    session.auth = (env("WOO_CONSUMER_KEY"), env("WOO_CONSUMER_SECRET"))

    print("=" * 62)
    print("PROBE 1 - credentials and reachability")
    print("=" * 62)
    print(f"store        : {store_url}")
    print(f"timezone     : {tz_name}")
    probe = get(session, orders_url, {"per_page": 1, "status": ALL_STATUSES})
    print(f"http status  : {probe.status_code}")
    if probe.status_code != 200:
        print(f"body         : {probe.text[:400]}")
        sys.exit(1)
    print(f"orders total : {probe.headers.get('X-WP-Total')}")
    sample: list[dict[str, Any]] = probe.json()
    if not sample:
        print("[FATAL] store returned zero orders")
        sys.exit(1)
    newest = sample[0]
    print(f"newest id    : {newest['id']}")
    print(f"date_created : {newest['date_created']}")
    print(f"date_gmt     : {newest['date_created_gmt']}")
    print(f"currency     : {newest['currency']}")

    print()
    print("=" * 62)
    print("PROBE 2 - dates_are_gmt on a 'today' window")
    print("=" * 62)
    now_local = datetime.now(tz)
    day_start = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    after_shifted = utc_str(day_start - timedelta(seconds=1))
    before_utc = utc_str(day_end)
    print(f"local window : {day_start.isoformat()} .. {day_end.isoformat()}")
    print(f"utc window   : {after_shifted} .. {before_utc}")
    gmt_count = count_orders(session, orders_url, after_shifted, before_utc, True)
    site_count = count_orders(session, orders_url, after_shifted, before_utc, False)
    print(f"dates_are_gmt=true  : {gmt_count} orders")
    print(f"dates_are_gmt=false : {site_count} orders")

    print()
    print("=" * 62)
    print("PROBE 3 - are after/before bounds exclusive?")
    print("=" * 62)
    anchor = newest["date_created_gmt"]
    anchor_dt = datetime.fromisoformat(anchor).replace(tzinfo=ZoneInfo("UTC"))
    exact = count_orders(session, orders_url, anchor, None, True)
    minus_one = count_orders(
        session, orders_url, utc_str(anchor_dt - timedelta(seconds=1)), None, True
    )
    print(f"anchor (gmt)        : {anchor}")
    print(f"after == anchor     : {exact} orders")
    print(f"after == anchor -1s : {minus_one} orders")
    print("expected: the anchor order itself appears only in the second count")

    print()
    print("=" * 62)
    print("PROBE 4 - status breakdown and refund shape, last 7 days")
    print("=" * 62)
    week_start = day_start - timedelta(days=6)
    week_after = utc_str(week_start - timedelta(seconds=1))
    orders = fetch_all(session, orders_url, week_after, before_utc)
    print(f"window (local): {week_start.date()} .. {day_start.date()}")
    print(f"fetched       : {len(orders)} orders")

    tally: dict[str, list[float]] = {}
    with_refunds: list[dict[str, Any]] = []
    for order in orders:
        status = str(order["status"])
        tally.setdefault(status, []).append(float(order["total"]))
        if order.get("refunds"):
            with_refunds.append(order)

    print()
    print(f"{'status':<12}{'count':>7}{'sum(total)':>14}")
    print("-" * 33)
    for status in sorted(tally):
        totals = tally[status]
        print(f"{status:<12}{len(totals):>7}{sum(totals):>14.2f}")

    print()
    print(f"orders carrying a refunds array: {len(with_refunds)}")
    if with_refunds:
        example = with_refunds[0]
        print(f"example order id : {example['id']}")
        print(f"example status   : {example['status']}")
        print(f"refunds payload  : {example['refunds']}")
        print(f"refunds[0] keys  : {sorted(example['refunds'][0].keys())}")
    else:
        print("[WARN] no refunded orders in this window - widen it before Step 11.3")

    print()
    print(f"tls verification used: {_verify_tls}")


if __name__ == "__main__":
    main()
