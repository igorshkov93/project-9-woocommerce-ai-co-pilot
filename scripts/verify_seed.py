"""Final verification of the seeded store data.

Reads the store the way the future n8n tools will read it and prints the
numbers they are expected to produce. Any mismatch is cheaper to find here than
inside a workflow, where a data problem looks identical to a schema problem.

Three contracts are asserted rather than described:

* Revenue excludes cancelled, failed and refunded orders. WooCommerce flips an
  order to "refunded" on its own once the refunded amount matches the total,
  which happens whenever a single-item order is returned.
* Returns are counted from the line items of the refund, never from the line
  items of the parent order. A basket containing earbuds and a t-shirt where
  only the earbuds came back must not credit a return to the t-shirt.
* Day boundaries come from BUSINESS_TIMEZONE. The naive UTC count is printed
  next to it so any divergence stays visible.

Read-only.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from woo_client import WooClient, WooError

# Orders that never turned into money.
NON_REVENUE_STATUSES = ("cancelled", "failed", "refunded")


def to_local(stamp: str, tz: ZoneInfo) -> datetime:
    """Convert a GMT timestamp string from the API into business-local time."""
    return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).astimezone(tz)


def main() -> int:
    try:
        client = WooClient()
        tz = ZoneInfo(client.env["BUSINESS_TIMEZONE"])
        now_local = datetime.now(tz)
        today = now_local.date()

        orders = client.get_all("orders", {"status": "any"})
        products = client.get_all("products", {"status": "publish"})

        print("=== CATALOG ===")
        categories = client.get_all("products/categories", {"slug": "electronics"})
        electronics_id = categories[0]["id"] if categories else None
        print(f"products:            {len(products)}")
        print(f"electronics cat id:  {electronics_id}")
        print(f"electronics items:   {categories[0]['count'] if categories else 0}")

        print()
        print("=== ORDERS ===")
        statuses = Counter(o["status"] for o in orders)
        print(f"total orders:        {len(orders)}")
        for status, count in statuses.most_common():
            print(f"  {status:<12} {count:>4}")

        by_day: dict[str, int] = defaultdict(int)
        revenue_by_day: dict[str, float] = defaultdict(float)
        for order in orders:
            local = to_local(order["date_created_gmt"], tz)
            key = local.date().isoformat()
            by_day[key] += 1
            if order["status"] not in NON_REVENUE_STATUSES:
                revenue_by_day[key] += float(order["total"])

        print()
        print(f"distinct days:       {len(by_day)}")
        print(f"date range (local):  {min(by_day)} .. {max(by_day)}")

        print()
        print("=== TIMEZONE CONTRACT ===")
        local_today = sum(
            1 for o in orders if to_local(o["date_created_gmt"], tz).date() == today
        )
        naive_today = sum(
            1
            for o in orders
            if datetime.fromisoformat(o["date_created_gmt"]).date() == today
        )
        night_today = sum(
            1
            for o in orders
            if to_local(o["date_created_gmt"], tz).date() == today
            and to_local(o["date_created_gmt"], tz).hour < 3
        )
        print(f"business timezone:   {client.env['BUSINESS_TIMEZONE']}")
        print(f"night orders today:  {night_today}")
        print(f"orders today (local):{local_today:>4}   <- correct answer")
        print(f"orders today (UTC):  {naive_today:>4}   <- what a naive tool returns")

        print()
        print("=== REVENUE (last 7 local days) ===")
        for offset in range(6, -1, -1):
            key = (today - timedelta(days=offset)).isoformat()
            print(
                f"  {key}  orders={by_day.get(key, 0):>3}  "
                f"revenue={revenue_by_day.get(key, 0.0):>9.2f}"
            )

        excluded = sum(
            float(o["total"]) for o in orders if o["status"] in NON_REVENUE_STATUSES
        )
        print()
        print(f"revenue total (60d): {sum(revenue_by_day.values()):.2f}")
        print(f"excluded (cancelled/failed/refunded): {excluded:.2f}")

        print()
        print("=== RETURNS ===")
        returned: Counter[str] = Counter()
        refund_total = 0.0
        refund_count = 0

        for order in orders:
            refunds = order.get("refunds") or []
            if not refunds:
                continue
            refund_count += len(refunds)

            for refund in refunds:
                refund_total += abs(float(refund.get("total") or 0))
                # Fetch the refund itself: its line items are the products that
                # actually came back. The parent order lists the whole basket.
                detail = client.get(f"orders/{order['id']}/refunds/{refund['id']}")
                for item in detail.get("line_items", []):
                    quantity = abs(int(item.get("quantity") or 0))
                    returned[item["name"]] += max(quantity, 1)

        print(f"orders with refunds: {sum(1 for o in orders if o.get('refunds'))}")
        print(f"refund records:      {refund_count}")
        print(f"refunded amount:     {refund_total:.2f}")

        print()
        print("top returned products:")
        for name, count in returned.most_common(5):
            print(f"  {count:>3}  {name}")

        electronics_returns = sum(
            count for name, count in returned.items()
            if any(token in name for token in ("Earbuds", "Watch", "Speaker", "Power Bank", "USB-C", "Mouse"))
        )
        print()
        print(f"electronics share of returns: {electronics_returns}/{sum(returned.values())}")

        print()
        print("=== VERDICT ===")
        problems = []
        if len(by_day) < 55:
            problems.append(f"expected ~60 distinct days, found {len(by_day)}")
        if refund_count == 0:
            problems.append("no refunds found")
        if electronics_id is None:
            problems.append("electronics category missing")
        if local_today == 0:
            problems.append("no orders today, the daily summary demo will be empty")
        if local_today == naive_today:
            problems.append(
                "local and UTC counts agree today: the timezone regression is not covered"
            )
        if sum(returned.values()) != refund_count:
            problems.append(
                f"returned items ({sum(returned.values())}) do not match refunds ({refund_count})"
            )

        if problems:
            for problem in problems:
                print(f"  PROBLEM: {problem}")
            return 1

        print("  All checks passed. The store is ready for Phase 2.")

    except WooError as error:
        print(f"FAIL: {error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())