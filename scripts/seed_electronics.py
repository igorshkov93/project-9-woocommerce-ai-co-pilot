"""Create the Electronics category and its products.

The demo catalog that ships with WooCommerce contains only apparel, so the
"15% off Electronics" coupon and the returns report would have nothing to act
on. This script adds a small, realistic electronics line.

Idempotent: products are matched by SKU and the category by slug, so running it
twice changes nothing. Read-only by default; pass --apply to write.

Stock management is intentionally disabled on these products. The order
generator creates completed orders, which would otherwise decrement stock and
eventually push items out of stock mid-run.
"""

from __future__ import annotations

import argparse
import sys

from woo_client import WooClient, WooError

CATEGORY = {
    "name": "Electronics",
    "slug": "electronics",
    "description": (
        "Consumer electronics: wireless audio, wearables, charging and "
        "connectivity accessories."
    ),
}

PRODUCTS: list[dict[str, str]] = [
    {
        "sku": "ELEC-EARBUD-01",
        "name": "Wireless Earbuds Pro",
        "regular_price": "79.00",
        "short_description": "True wireless earbuds with active noise cancellation.",
        "description": (
            "Bluetooth 5.3 earbuds with hybrid active noise cancellation, six "
            "hours of playback per charge and twenty-four hours from the case. "
            "IPX4 water resistance for workouts and commuting."
        ),
    },
    {
        "sku": "ELEC-WATCH-01",
        "name": "Smart Watch Series 5",
        "regular_price": "199.00",
        "short_description": "Fitness smartwatch with heart rate and GPS tracking.",
        "description": (
            "A 1.4 inch AMOLED smartwatch with continuous heart rate monitoring, "
            "built-in GPS, sleep tracking and seven days of battery life. "
            "Compatible with both Android and iOS."
        ),
    },
    {
        "sku": "ELEC-SPEAKER-01",
        "name": "Portable Bluetooth Speaker",
        "regular_price": "59.00",
        "short_description": "Rugged portable speaker with twelve hour battery.",
        "description": (
            "A compact 20 watt speaker with IP67 dust and water protection, "
            "twelve hours of playback and stereo pairing for two units. "
            "Designed for outdoor use."
        ),
    },
    {
        "sku": "ELEC-POWER-01",
        "name": "Power Bank 20000mAh",
        "regular_price": "45.00",
        "short_description": "High capacity power bank with fast USB-C charging.",
        "description": (
            "A 20000 mAh battery pack with 65 watt USB-C Power Delivery, enough "
            "to charge a laptop or refill a phone four times. Includes a "
            "pass-through charging mode."
        ),
    },
    {
        "sku": "ELEC-HUB-01",
        "name": "USB-C Hub 7-in-1",
        "regular_price": "39.00",
        "short_description": "Seven port USB-C hub with HDMI and card readers.",
        "description": (
            "Expands a single USB-C port into HDMI 4K, two USB-A 3.0 ports, "
            "SD and microSD readers, Gigabit Ethernet and 100 watt pass-through "
            "power delivery."
        ),
    },
    {
        "sku": "ELEC-MOUSE-01",
        "name": "Wireless Ergonomic Mouse",
        "regular_price": "29.00",
        "short_description": "Silent vertical mouse for all-day desk work.",
        "description": (
            "A vertical ergonomic mouse with silent switches, adjustable DPI up "
            "to 4000 and a rechargeable battery lasting up to two months per "
            "charge."
        ),
    },
]


def ensure_category(client: WooClient, apply: bool) -> int | None:
    """Return the Electronics category id, creating it when needed."""
    existing = client.get("products/categories", {"slug": CATEGORY["slug"]})
    if existing:
        category = existing[0]
        print(f"category: exists  id={category['id']} slug={category['slug']}")
        return int(category["id"])

    if not apply:
        print(f"category: WOULD CREATE slug={CATEGORY['slug']}")
        return None

    created = client.post("products/categories", CATEGORY)
    print(f"category: CREATED id={created['id']} slug={created['slug']}")
    return int(created["id"])


def ensure_products(client: WooClient, category_id: int | None, apply: bool) -> None:
    """Create every missing product, matching on SKU."""
    for spec in PRODUCTS:
        existing = client.get("products", {"sku": spec["sku"]})
        if existing:
            product = existing[0]
            print(f"product:  exists  id={product['id']:<5} sku={spec['sku']}")
            continue

        if not apply or category_id is None:
            print(f"product:  WOULD CREATE sku={spec['sku']:<18} {spec['name']}")
            continue

        payload: dict[str, object] = dict(spec)
        payload.update(
            {
                "type": "simple",
                "status": "publish",
                "manage_stock": False,
                "stock_status": "instock",
                "categories": [{"id": category_id}],
            }
        )
        created = client.post("products", payload)
        print(
            f"product:  CREATED id={created['id']:<5} sku={spec['sku']:<18} {created['name']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the Electronics catalog line.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to the store. Without it the script only reports.",
    )
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"mode: {mode}")
    print()

    try:
        client = WooClient()
        category_id = ensure_category(client, args.apply)
        ensure_products(client, category_id, args.apply)
        print()
        print(f"products in store: {client.count('products')}")
    except WooError as error:
        print(f"FAIL: {error}")
        return 1

    if not args.apply:
        print()
        print("Nothing was written. Re-run with --apply to commit these changes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
