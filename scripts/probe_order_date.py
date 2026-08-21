"""Find out how to backdate an order through the WooCommerce REST API.

The batch create silently ignored date_created_gmt and stamped every order with
the current time. Three strategies are tested against real throwaway orders,
which are deleted again at the end:

  A  single create with date_created
  B  single create with date_created_gmt
  C  plain create, then update the date with PUT

The store runs at gmt_offset=0, so date_created and date_created_gmt carry the
same value here. Whichever strategy sticks is the one the generator will use.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from woo_client import WooClient, WooError

PROBE_MARKER_KEY = "_probe_order"
PROBE_MARKER_VALUE = "date-strategy"


def base_payload(product_id: int) -> dict[str, object]:
    return {
        "status": "completed",
        "payment_method": "bacs",
        "payment_method_title": "Direct bank transfer",
        "billing": {
            "first_name": "Probe",
            "last_name": "Tester",
            "email": "probe@example.com",
            "city": "Kyiv",
            "country": "UA",
        },
        "line_items": [{"product_id": product_id, "quantity": 1}],
        "meta_data": [{"key": PROBE_MARKER_KEY, "value": PROBE_MARKER_VALUE}],
    }


def main() -> int:
    try:
        client = WooClient()

        products = client.get("products", {"per_page": 1, "status": "publish"})
        product_id = int(products[0]["id"])
        print(f"probe product: {product_id} {products[0]['name']}")

        target_dt = datetime.now(timezone.utc) - timedelta(days=30)
        target = target_dt.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"target date:   {target}")
        print()

        created_ids: list[int] = []
        results: list[tuple[str, str, str]] = []

        # Strategy A: single create with date_created.
        payload = base_payload(product_id)
        payload["date_created"] = target
        order = client.post("orders", payload)
        created_ids.append(int(order["id"]))
        results.append(("A create date_created", str(order["id"]), str(order.get("date_created_gmt"))))

        # Strategy B: single create with date_created_gmt.
        payload = base_payload(product_id)
        payload["date_created_gmt"] = target
        order = client.post("orders", payload)
        created_ids.append(int(order["id"]))
        results.append(("B create date_created_gmt", str(order["id"]), str(order.get("date_created_gmt"))))

        # Strategy C: create first, then move the date with an update.
        order = client.post("orders", base_payload(product_id))
        order_id = int(order["id"])
        created_ids.append(order_id)
        updated = client.request("PUT", f"orders/{order_id}", payload={"date_created": target})[0]
        results.append(("C create then PUT", str(order_id), str(updated.get("date_created_gmt"))))

        print(f"{'strategy':<28} {'order':<8} resulting date_created_gmt")
        for label, order_id_str, resulting in results:
            verdict = "OK  " if resulting.startswith(target[:10]) else "FAIL"
            print(f"{verdict} {label:<23} {order_id_str:<8} {resulting}")

        print()
        for order_id in created_ids:
            client.delete(f"orders/{order_id}", {"force": "true"})
            print(f"cleaned up probe order {order_id}")

        print()
        print(f"orders in store: {client.count('orders', {'status': 'any'})}")

    except WooError as error:
        print(f"FAIL: {error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())