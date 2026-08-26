"""Diagnose why no orders qualify for a refund.

Prints the actual shape of the stored data: status spread, the real date range
and the reason each order was rejected. Read-only.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

from woo_client import WooClient, WooError

MIN_AGE_DAYS = 2


def main() -> int:
    try:
        client = WooClient()
        now_utc = datetime.now(UTC)
        orders = client.get_all("orders", {"status": "any"})
        print(f"orders fetched: {len(orders)}")
        print()

        statuses = Counter(o.get("status") for o in orders)
        print("statuses:")
        for status, total in statuses.most_common():
            print(f"  {status:<12} {total:>4}")
        print()

        dates = sorted(o.get("date_created_gmt") or "" for o in orders)
        distinct_days = {d[:10] for d in dates if d}
        print(f"earliest date_created_gmt: {dates[0] if dates else '-'}")
        print(f"latest   date_created_gmt: {dates[-1] if dates else '-'}")
        print(f"distinct calendar days:    {len(distinct_days)}")
        print()

        reasons: Counter[str] = Counter()
        for order in orders:
            if order.get("status") != "completed":
                reasons["status not completed"] += 1
                continue
            if order.get("refunds"):
                reasons["already refunded"] += 1
                continue
            created = order.get("date_created_gmt")
            if not created:
                reasons["no date_created_gmt"] += 1
                continue
            created_dt = datetime.fromisoformat(created).replace(tzinfo=UTC)
            if now_utc - created_dt < timedelta(days=MIN_AGE_DAYS):
                reasons["too recent"] += 1
                continue
            if not order.get("line_items"):
                reasons["no line items"] += 1
                continue
            reasons["ELIGIBLE"] += 1

        print("rejection breakdown:")
        for reason, total in reasons.most_common():
            print(f"  {reason:<22} {total:>4}")
        print()

        sample = next((o for o in orders if o.get("status") == "completed"), orders[0])
        print(f"sample order id={sample['id']}")
        for key in ("status", "currency", "total", "date_created", "date_created_gmt"):
            print(f"  {key:<18} {sample.get(key)}")
        print(f"  refunds            {json.dumps(sample.get('refunds'))}")
        print(f"  line_items count   {len(sample.get('line_items') or [])}")
        first_item = (sample.get("line_items") or [{}])[0]
        for key in ("id", "name", "sku", "quantity", "total", "total_tax"):
            print(f"    item.{key:<12} {first_item.get(key)}")

    except WooError as error:
        print(f"FAIL: {error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
