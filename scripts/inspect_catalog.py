"""Read-only inventory of the WooCommerce catalog.

Prints categories and products with the exact identifiers the order generator
will need. Creates and modifies nothing, so it is safe to run at any time.
"""

from __future__ import annotations

import sys

from woo_client import WooClient, WooError


def main() -> int:
    try:
        client = WooClient()

        print("=== CATEGORIES ===")
        categories = client.get_all("products/categories", {"hide_empty": "false"})
        if not categories:
            print("(none)")
        for category in sorted(categories, key=lambda c: c["name"]):
            print(
                f"id={category['id']:<5} "
                f"count={category['count']:<4} "
                f"slug={category['slug']:<24} "
                f"name={category['name']}"
            )

        print()
        print("=== PRODUCTS ===")
        products = client.get_all("products", {"status": "publish"})
        for product in sorted(products, key=lambda p: p["id"]):
            names = ", ".join(c["name"] for c in product.get("categories", [])) or "-"
            print(
                f"id={product['id']:<5} "
                f"type={product['type']:<10} "
                f"price={str(product.get('price') or '-'):<9} "
                f"stock={str(product.get('stock_status')):<12} "
                f"cats=[{names}] "
                f"name={product['name']}"
            )

            if product["type"] == "variable":
                variations = client.get_all(f"products/{product['id']}/variations")
                for variation in variations:
                    attributes = ", ".join(
                        f"{a.get('name')}={a.get('option')}"
                        for a in variation.get("attributes", [])
                    )
                    print(
                        f"    variation_id={variation['id']:<5} "
                        f"price={str(variation.get('price') or '-'):<9} "
                        f"[{attributes}]"
                    )

        print()
        print(f"totals: {len(products)} published products, {len(categories)} categories")

    except WooError as error:
        print(f"FAIL: {error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())