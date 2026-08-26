"""Create partial refunds against completed orders.

The "top returned products" report needs to name specific products, and a
refunded order status cannot do that: it only records that money went back,
not which line item was returned. WooCommerce models a real return as a refund
object with its own line items, so that is what this script creates.

Electronics are refunded more often than apparel, which mirrors real stores and
gives the report a meaningful top instead of a flat list.

Idempotent: orders that already have refunds are skipped. Read-only by default;
pass --apply to write.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

from woo_client import WooClient, WooError

SEED = 20260821
REFUND_RATE = 0.09
MIN_AGE_DAYS = 2

# Relative likelihood that a given line item is the one sent back.
ELECTRONICS_WEIGHTS = {
    "ELEC-EARBUD-01": 3.5,
    "ELEC-WATCH-01": 3.0,
    "ELEC-SPEAKER-01": 2.0,
    "ELEC-POWER-01": 1.5,
    "ELEC-HUB-01": 2.5,
    "ELEC-MOUSE-01": 1.5,
}
DEFAULT_ITEM_WEIGHT = 1.0

REASONS = [
    "Item did not match the description",
    "Arrived damaged",
    "Wrong size",
    "Customer changed their mind",
    "Faulty on arrival",
    "Better price found elsewhere",
    "Battery life below expectations",
    "Difficult to set up",
]


def item_weight(sku: str) -> float:
    return ELECTRONICS_WEIGHTS.get(sku, DEFAULT_ITEM_WEIGHT)


def eligible(order: dict, now_utc: datetime) -> bool:
    """Only settled orders that are old enough can be returned."""
    if order.get("status") != "completed":
        return False
    if order.get("refunds"):
        return False

    created = order.get("date_created_gmt")
    if not created:
        return False
    created_dt = datetime.fromisoformat(created).replace(tzinfo=UTC)
    if now_utc - created_dt < timedelta(days=MIN_AGE_DAYS):
        return False

    return bool(order.get("line_items"))


def pick_line_item(rng: random.Random, order: dict) -> dict | None:
    """Choose which line item the customer sent back."""
    items = [i for i in order["line_items"] if float(i.get("total") or 0) > 0]
    if not items:
        return None
    weights = [item_weight(i.get("sku") or "") for i in items]
    return rng.choices(items, weights=weights, k=1)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate partial refunds.")
    parser.add_argument(
        "--apply", action="store_true", help="Write refunds to the store."
    )
    args = parser.parse_args()

    print(f"mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print()

    try:
        client = WooClient()
        rng = random.Random(SEED)
        now_utc = datetime.now(UTC)

        orders = client.get_all("orders", {"status": "any"})
        candidates = [o for o in orders if eligible(o, now_utc)]
        already = sum(1 for o in orders if o.get("refunds"))

        target = round(len(orders) * REFUND_RATE)
        todo = max(0, target - already)
        chosen = rng.sample(candidates, min(todo, len(candidates)))

        print(f"orders total:        {len(orders)}")
        print(f"eligible:            {len(candidates)}")
        print(f"already refunded:    {already}")
        print(f"refunds to create:   {len(chosen)}")
        print()

        if not chosen:
            print("Nothing to do.")
            return 0

        planned: Counter[str] = Counter()
        created = 0

        for order in sorted(chosen, key=lambda o: o["id"]):
            line_item = pick_line_item(rng, order)
            if line_item is None:
                continue

            unit_total = float(line_item["total"]) / max(int(line_item["quantity"]), 1)
            unit_tax = float(line_item.get("total_tax") or 0) / max(
                int(line_item["quantity"]), 1
            )
            amount = round(unit_total + unit_tax, 2)
            if amount <= 0:
                continue

            reason = rng.choice(REASONS)
            planned[line_item["name"]] += 1

            print(
                f"order={order['id']:<6} "
                f"item={line_item['name'][:38]:<38} "
                f"amount={amount:<8} "
                f"reason={reason}"
            )

            if not args.apply:
                continue

            payload = {
                "amount": f"{amount:.2f}",
                "reason": reason,
                "api_refund": False,
                "line_items": [
                    {
                        "id": line_item["id"],
                        "quantity": 1,
                        "refund_total": round(unit_total, 2),
                    }
                ],
            }
            client.post(f"orders/{order['id']}/refunds", payload)
            created += 1

        print()
        print("refunds by product:")
        for name, total in planned.most_common():
            print(f"  {total:>3}  {name}")

        if args.apply:
            print()
            print(f"refunds created: {created}")
        else:
            print()
            print("Nothing was written. Re-run with --apply to commit.")

    except WooError as error:
        print(f"FAIL: {error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
