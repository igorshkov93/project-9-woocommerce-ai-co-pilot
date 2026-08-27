"""Explain why refund totals differ between orders_summary and top_returned.

orders_summary.py answers a revenue question: it must not subtract a refund
from an order whose revenue was already excluded, or the money is deducted
twice. top_returned_products.py answers an inventory question: a unit that
came back came back, whatever the parent order's status.

This probe groups refunds by parent order status so the gap between the two
figures is a measured fact rather than a plausible story.

Read-only. Uses the refunds array embedded in GET /orders, which carries a
total per refund, so no detail call is needed here.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from woo_client import WooClient, WooError

REVENUE_STATUSES = ("pending", "processing", "on-hold", "completed")
SEPARATOR = "-" * 62


def to_cents(value: Any) -> int:
    """Convert a money string to absolute integer cents."""
    try:
        return abs(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def utc_bounds(from_date: str, to_date: str, tz: ZoneInfo) -> tuple[str, str]:
    """Build exclusive UTC bounds from inclusive local dates."""
    start = datetime.strptime(from_date, "%Y-%m-%d").date()
    end = datetime.strptime(to_date, "%Y-%m-%d").date()
    start_local = datetime.combine(start, datetime.min.time(), tzinfo=tz)
    end_local = datetime.combine(
        end + timedelta(days=1), datetime.min.time(), tzinfo=tz
    )
    utc = ZoneInfo("UTC")
    after = (start_local.astimezone(utc) - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    before = end_local.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%S")
    return after, before


def main() -> int:
    parser = argparse.ArgumentParser(description="Refund attribution breakdown")
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", required=True)
    args = parser.parse_args()

    try:
        client = WooClient()
        tz = ZoneInfo(client.env["BUSINESS_TIMEZONE"])
        after, before = utc_bounds(args.from_date, args.to_date, tz)
        orders = client.get_all(
            "orders",
            {
                "status": "any",
                "after": after,
                "before": before,
                "dates_are_gmt": "true",
            },
        )
    except (WooError, ValueError, KeyError) as error:
        print(f"FAIL: {error}")
        return 1

    by_status: Counter[str] = Counter()
    cents_by_status: Counter[str] = Counter()
    entries_by_status: Counter[str] = Counter()

    for order in orders:
        refunds = order.get("refunds") or []
        if not refunds:
            continue
        status = str(order.get("status", "unknown"))
        by_status[status] += 1
        for entry in refunds:
            entries_by_status[status] += 1
            cents_by_status[status] += to_cents(entry.get("total"))

    print(SEPARATOR)
    print(f"window {args.from_date} .. {args.to_date}, orders {len(orders)}")
    print(SEPARATOR)
    print(f"{'status':<14}{'orders':>8}{'refunds':>9}{'amount':>12}")
    for status in sorted(by_status):
        print(
            f"{status:<14}{by_status[status]:>8}"
            f"{entries_by_status[status]:>9}"
            f"{cents_by_status[status] / 100:>12.2f}"
        )

    total = sum(cents_by_status.values())
    revenue_side = sum(
        cents for status, cents in cents_by_status.items() if status in REVENUE_STATUSES
    )
    excluded = total - revenue_side

    print(SEPARATOR)
    print(f"all refunds in window:            {total / 100:>10.2f}")
    print(f"on revenue-counted orders:        {revenue_side / 100:>10.2f}")
    print(f"on already-excluded orders:       {excluded / 100:>10.2f}")
    print(SEPARATOR)
    print("expectation: the middle figure equals the orders_summary refunds")
    print("total, and the bottom figure is the gap to top_returned_products")
    return 0


if __name__ == "__main__":
    sys.exit(main())
