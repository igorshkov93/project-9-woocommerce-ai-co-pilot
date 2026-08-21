"""Generate roughly sixty days of realistic orders for the demo store.

Every downstream tool depends on this data: the daily summary, the returns
report and the Slack digest all read what this script writes.

Design notes:

* Products are discovered through the API, never hardcoded. Grouped and
  external products cannot be purchased, so they are excluded; variable
  products are expanded into their individual variations.
* Timestamps are generated in the business timezone and converted to UTC before
  being sent as date_created_gmt. A deliberate share of orders lands between
  00:00 and 03:00 local time, which belongs to the previous day in UTC. Those
  orders are the regression test for the daily summary tool.
* Statuses depend on how old an order is: recent orders are mostly still being
  processed, older ones are completed. Refunds are not created here, see the
  dedicated refunds script.
* Idempotency relies on a marker written into order meta. Orders cannot be
  matched by SKU the way products can.
* Batches are small because LocalWP is slow: a write that times out cannot be
  retried safely, since the server has probably applied it already.

Read-only by default; pass --apply to write.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from woo_client import WooClient, WooError

SEED = 20260821
DAYS = 60
BATCH_SIZE = 5
SEED_MARKER_KEY = "_seed_batch"
SEED_MARKER_VALUE = "orders-v1"

# Share of orders placed between midnight and 03:00 local time. In UTC these
# belong to the previous calendar day.
NIGHT_ORDER_RATIO = 0.08

EXCLUDED_TYPES = ("grouped", "external")

# Higher weights make the returns report meaningful: uniform demand would
# spread refunds evenly and reveal nothing.
SKU_WEIGHTS = {
    "ELEC-EARBUD-01": 9,
    "ELEC-WATCH-01": 7,
    "ELEC-SPEAKER-01": 5,
    "ELEC-POWER-01": 5,
    "ELEC-HUB-01": 4,
    "ELEC-MOUSE-01": 4,
}
DEFAULT_WEIGHT = 3

CUSTOMERS = [
    ("Olena", "Kovalenko", "olena.kovalenko@example.com", "Kyiv", "UA"),
    ("Michael", "Brennan", "michael.brennan@example.com", "Austin", "US"),
    ("Sofia", "Marchetti", "sofia.marchetti@example.com", "Milan", "IT"),
    ("James", "Whitfield", "james.whitfield@example.com", "Leeds", "GB"),
    ("Anna", "Lindqvist", "anna.lindqvist@example.com", "Malmo", "SE"),
    ("Daniel", "Okafor", "daniel.okafor@example.com", "Toronto", "CA"),
    ("Marta", "Nowak", "marta.nowak@example.com", "Krakow", "PL"),
    ("Ethan", "Sullivan", "ethan.sullivan@example.com", "Portland", "US"),
    ("Yuki", "Tanaka", "yuki.tanaka@example.com", "Osaka", "JP"),
    ("Laura", "Fernandez", "laura.fernandez@example.com", "Valencia", "ES"),
    ("Tomas", "Novak", "tomas.novak@example.com", "Brno", "CZ"),
    ("Grace", "Adeyemi", "grace.adeyemi@example.com", "Manchester", "GB"),
]

PAYMENT_METHODS = [
    ("bacs", "Direct bank transfer"),
    ("cod", "Cash on delivery"),
    ("cheque", "Check payments"),
]


class PoolItem:
    """One purchasable line item candidate."""

    def __init__(
        self,
        product_id: int,
        variation_id: int | None,
        name: str,
        sku: str,
        weight: int,
    ) -> None:
        self.product_id = product_id
        self.variation_id = variation_id
        self.name = name
        self.sku = sku
        self.weight = weight

    def as_line_item(self, quantity: int) -> dict[str, object]:
        item: dict[str, object] = {"product_id": self.product_id, "quantity": quantity}
        if self.variation_id:
            item["variation_id"] = self.variation_id
        return item


def build_product_pool(client: WooClient) -> list[PoolItem]:
    """Discover every purchasable product and variation."""
    pool: list[PoolItem] = []
    products = client.get_all("products", {"status": "publish"})

    for product in products:
        if product["type"] in EXCLUDED_TYPES:
            continue
        if product.get("stock_status") != "instock":
            continue

        weight = SKU_WEIGHTS.get(product.get("sku") or "", DEFAULT_WEIGHT)

        if product["type"] == "variable":
            for variation in client.get_all(f"products/{product['id']}/variations"):
                options = ", ".join(
                    str(a.get("option")) for a in variation.get("attributes", [])
                )
                pool.append(
                    PoolItem(
                        product_id=int(product["id"]),
                        variation_id=int(variation["id"]),
                        name=f"{product['name']} ({options})",
                        sku=variation.get("sku") or product.get("sku") or "",
                        weight=weight,
                    )
                )
        else:
            pool.append(
                PoolItem(
                    product_id=int(product["id"]),
                    variation_id=None,
                    name=product["name"],
                    sku=product.get("sku") or "",
                    weight=weight,
                )
            )

    return pool


def orders_for_day(rng: random.Random, day: datetime, days_ago: int) -> int:
    """Order volume for one day, with a weekend bump and a growth trend."""
    weekend = day.weekday() >= 5
    base = rng.randint(3, 5) if weekend else rng.randint(2, 4)
    growth = 1.0 + 0.4 * (1.0 - days_ago / max(DAYS - 1, 1))
    return max(1, round(base * growth))


def pick_status(rng: random.Random, days_ago: int) -> str:
    """Recent orders are still in flight, older ones have settled."""
    if days_ago == 0:
        choices, weights = (
            ["processing", "on-hold", "completed", "cancelled"],
            [50, 15, 25, 10],
        )
    elif days_ago <= 2:
        choices, weights = (
            ["processing", "completed", "on-hold", "cancelled"],
            [35, 50, 5, 10],
        )
    else:
        choices, weights = (
            ["completed", "processing", "cancelled", "on-hold", "failed"],
            [80, 5, 9, 4, 2],
        )
    return rng.choices(choices, weights=weights, k=1)[0]


def pick_local_time(rng: random.Random, day: datetime, force_night: bool) -> datetime:
    """Return a local timestamp inside the given day."""
    if force_night or rng.random() < NIGHT_ORDER_RATIO:
        hour = rng.choice([0, 1, 2])
    else:
        hour = rng.choices(
            [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22],
            weights=[4, 6, 7, 8, 7, 6, 6, 7, 8, 9, 10, 9, 6, 4],
            k=1,
        )[0]
    return day.replace(
        hour=hour,
        minute=rng.randint(0, 59),
        second=rng.randint(0, 59),
        microsecond=0,
    )


def build_orders(
    rng: random.Random, pool: list[PoolItem], tz: ZoneInfo
) -> list[tuple[datetime, dict[str, object]]]:
    """Build every order payload together with its local timestamp."""
    weights = [item.weight for item in pool]
    today_local = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
    orders: list[tuple[datetime, dict[str, object]]] = []

    for days_ago in range(DAYS - 1, -1, -1):
        day = today_local - timedelta(days=days_ago)
        count = orders_for_day(rng, day, days_ago)

        for index in range(count):
            # Guarantee at least one night order today so the timezone
            # behaviour of the summary tool is always exercised.
            force_night = days_ago == 0 and index == 0
            local_dt = pick_local_time(rng, day, force_night)
            utc_dt = local_dt.astimezone(timezone.utc)

            chosen: list[PoolItem] = []
            for _ in range(rng.choices([1, 2, 3], weights=[55, 30, 15], k=1)[0]):
                candidate = rng.choices(pool, weights=weights, k=1)[0]
                if candidate not in chosen:
                    chosen.append(candidate)

            line_items = [
                item.as_line_item(rng.choices([1, 2], weights=[85, 15], k=1)[0])
                for item in chosen
            ]

            first, last, email, city, country = rng.choice(CUSTOMERS)
            method, method_title = rng.choice(PAYMENT_METHODS)

            payload: dict[str, object] = {
                "status": pick_status(rng, days_ago),
                "date_created_gmt": utc_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "payment_method": method,
                "payment_method_title": method_title,
                "billing": {
                    "first_name": first,
                    "last_name": last,
                    "email": email,
                    "city": city,
                    "country": country,
                },
                "shipping": {
                    "first_name": first,
                    "last_name": last,
                    "city": city,
                    "country": country,
                },
                "line_items": line_items,
                "meta_data": [{"key": SEED_MARKER_KEY, "value": SEED_MARKER_VALUE}],
            }
            orders.append((local_dt, payload))

    return orders


def find_seeded(client: WooClient) -> list[int]:
    """Return the ids of orders that carry the seed marker."""
    seeded: list[int] = []
    for order in client.get_all("orders", {"status": "any"}):
        for meta in order.get("meta_data", []):
            if meta.get("key") == SEED_MARKER_KEY:
                seeded.append(int(order["id"]))
                break
    return seeded


def delete_seeded(client: WooClient, order_ids: list[int]) -> None:
    """Permanently remove previously seeded orders."""
    for order_id in order_ids:
        client.delete(f"orders/{order_id}", {"force": "true"})
        print(f"reset: deleted order {order_id}")


def report(orders: list[tuple[datetime, dict[str, object]]], tz: ZoneInfo) -> None:
    """Print the distribution the generator is about to create."""
    statuses = Counter(str(payload["status"]) for _, payload in orders)
    print("status distribution:")
    for status, total in statuses.most_common():
        print(f"  {status:<12} {total:>4}")

    today_local = datetime.now(tz).date()
    today_local_count = sum(1 for dt, _ in orders if dt.date() == today_local)
    today_utc_count = sum(
        1 for dt, _ in orders if dt.astimezone(timezone.utc).date() == today_local
    )
    night = sum(1 for dt, _ in orders if dt.hour < 3)

    print()
    print(f"total orders:          {len(orders)}")
    print(f"night orders (00-03):  {night}")
    print(f"orders today (local):  {today_local_count}")
    print(f"orders today (naive UTC): {today_utc_count}  <- wrong answer if timezone is ignored")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate demo orders.")
    parser.add_argument("--apply", action="store_true", help="Write orders to the store.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete previously seeded orders before writing.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write even if seeded orders already exist. May create duplicates.",
    )
    args = parser.parse_args()

    print(f"mode: {'APPLY' if args.apply else 'DRY-RUN'}")

    try:
        client = WooClient()
        tz = ZoneInfo(client.env["BUSINESS_TIMEZONE"])
        rng = random.Random(SEED)

        pool = build_product_pool(client)
        print(f"purchasable pool: {len(pool)} items")
        electronics = [i for i in pool if i.sku.startswith("ELEC-")]
        print(f"  electronics:    {len(electronics)}")
        print(f"  other:          {len(pool) - len(electronics)}")
        print()

        if not pool:
            print("FAIL: no purchasable products found.")
            return 1

        orders = build_orders(rng, pool, tz)
        report(orders, tz)

        seeded = find_seeded(client)
        print()
        print(f"already seeded in store: {len(seeded)}")

        if seeded and args.reset:
            if not args.apply:
                print(f"WOULD DELETE {len(seeded)} seeded orders before writing.")
            else:
                print()
                delete_seeded(client, seeded)
                seeded = []

        if seeded and not args.force:
            print("Seeded orders already exist. Refusing to run again. Use --reset or --force.")
            return 0

        if not args.apply:
            print()
            print("Nothing was written. Re-run with --apply to commit.")
            return 0

        print()
        created = 0
        total_batches = (len(orders) + BATCH_SIZE - 1) // BATCH_SIZE
        for start in range(0, len(orders), BATCH_SIZE):
            chunk = [payload for _, payload in orders[start : start + BATCH_SIZE]]
            result = client.post("orders/batch", {"create": chunk})
            for item in result.get("create", []):
                if item.get("id"):
                    created += 1
                else:
                    print(f"  error: {item.get('error', {}).get('message')}")
            print(f"batch {start // BATCH_SIZE + 1}/{total_batches}: {created} created")

        print()
        print(f"orders in store: {client.count('orders', {'status': 'any'})}")

    except WooError as error:
        print(f"FAIL: {error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())