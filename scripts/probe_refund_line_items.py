"""Probe the shape of WooCommerce refund objects before implementing metrics.

Read-only. Answers three questions that the top-returned-products tool
depends on and that must not be assumed:

1. Does the `refunds` array embedded in GET /orders carry line_items?
   If it does, the N+1 detail call is unnecessary. If it does not, the
   detail call is the only way to learn which product was returned.
2. Are refund line item `quantity` and `total` stored negative?
   WooCommerce documents them as negative, but a wrong assumption here
   would be masked by abs() on this dataset and surface on another.
3. How are `sku` and `reason` represented: missing key, null, or empty
   string? Each needs different normalisation.

The detail call is issued only for orders whose embedded refunds array is
non-empty, which is what keeps N bound to the number of refunds rather
than the number of orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any

from woo_client import WooClient, WooError

ORDER_FIELDS = "id,status,date_created_gmt,refunds"
SEPARATOR = "-" * 66


def sign_of(value: float) -> str:
    """Classify a number so signs can be counted rather than eyeballed."""
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "zero"


def describe(value: Any) -> str:
    """Distinguish a missing key from null from an empty string."""
    if value is None:
        return "null"
    if isinstance(value, str):
        return "empty-string" if value.strip() == "" else "non-empty-string"
    return type(value).__name__


def to_float(value: Any) -> float:
    """WooCommerce returns money as strings; treat unparsable as zero."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fetch_orders(client: WooClient) -> list[dict[str, Any]]:
    """Fetch all orders, trimmed to the fields this probe needs."""
    params = {"status": "any", "_fields": ORDER_FIELDS}
    orders = client.get_all("orders", params)
    if orders and "refunds" not in orders[0]:
        print("NOTE: _fields was ignored by the server, refetching in full")
        orders = client.get_all("orders", {"status": "any"})
    return [order for order in orders if isinstance(order, dict)]


def report_embedded(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inspect the refunds array embedded in the orders collection."""
    with_refunds = [order for order in orders if order.get("refunds")]
    embedded_keys: Counter[str] = Counter()
    embedded_count = 0

    for order in with_refunds:
        for entry in order["refunds"]:
            embedded_count += 1
            if isinstance(entry, dict):
                embedded_keys.update(entry.keys())

    print(SEPARATOR)
    print("EMBEDDED refunds array (from GET /orders)")
    print(SEPARATOR)
    print(f"orders fetched:            {len(orders)}")
    print(f"orders with refunds:       {len(with_refunds)}")
    print(f"embedded refund entries:   {embedded_count}")
    print("keys seen on entries:")
    for key, count in sorted(embedded_keys.items()):
        print(f"  {key:<20} {count}")
    has_items = "line_items" in embedded_keys
    verdict = "NOT NEEDED" if has_items else "REQUIRED"
    print(f"line_items present:        {has_items}")
    print(f"=> N+1 detail call is      {verdict}")
    return with_refunds


def probe_details(
    client: WooClient, with_refunds: list[dict[str, Any]], dump: int
) -> None:
    """Fetch refund details and aggregate the shape of their line items."""
    qty_signs: Counter[str] = Counter()
    total_signs: Counter[str] = Counter()
    item_keys: Counter[str] = Counter()
    sku_shapes: Counter[str] = Counter()
    reason_shapes: Counter[str] = Counter()
    refund_dates: Counter[str] = Counter()

    refund_count = 0
    empty_line_items = 0
    zero_qty_nonzero_total = 0
    mismatched_totals = 0
    dumped = 0

    for order in with_refunds:
        order_id = order["id"]
        refunds = client.get(f"orders/{order_id}/refunds", {"per_page": 100})
        if not isinstance(refunds, list):
            continue

        for refund in refunds:
            refund_count += 1
            date_gmt = str(refund.get("date_created_gmt", ""))[:10]
            refund_dates[date_gmt] += 1
            reason_shapes[describe(refund.get("reason", "__missing__"))] += 1

            line_items = refund.get("line_items") or []
            if not line_items:
                empty_line_items += 1

            line_sum = 0.0
            for item in line_items:
                item_keys.update(item.keys())
                quantity = to_float(item.get("quantity"))
                total = to_float(item.get("total"))
                line_sum += total
                qty_signs[sign_of(quantity)] += 1
                total_signs[sign_of(total)] += 1
                sku_shapes[describe(item.get("sku", "__missing__"))] += 1
                if quantity == 0 and total != 0:
                    zero_qty_nonzero_total += 1

            refund_total = to_float(refund.get("total"))
            if abs(abs(line_sum) - abs(refund_total)) > 0.01:
                mismatched_totals += 1

            if dumped < dump:
                print(SEPARATOR)
                print(f"RAW refund on order {order_id}")
                print(SEPARATOR)
                print(json.dumps(refund, indent=2, ensure_ascii=False)[:2400])
                dumped += 1

    print(SEPARATOR)
    print("DETAIL calls (GET /orders/<id>/refunds)")
    print(SEPARATOR)
    print(f"detail requests issued:    {len(with_refunds)}")
    print(f"refund objects returned:   {refund_count}")
    print(f"refunds w/o line_items:    {empty_line_items}")
    print(f"qty 0 but total non-zero:  {zero_qty_nonzero_total}")
    print(f"line sum != refund total:  {mismatched_totals}")
    print(f"quantity signs:            {dict(qty_signs)}")
    print(f"line total signs:          {dict(total_signs)}")
    print(f"sku shapes:                {dict(sku_shapes)}")
    print(f"reason shapes:             {dict(reason_shapes)}")
    print("line item keys:")
    for key, count in sorted(item_keys.items()):
        print(f"  {key:<24} {count}")
    print("refund date_created_gmt distribution:")
    for day, count in sorted(refund_dates.items()):
        print(f"  {day:<12} {count}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dump",
        type=int,
        default=2,
        help="how many raw refund objects to print in full (default: 2)",
    )
    args = parser.parse_args()

    try:
        client = WooClient()
        print(f"store:      {client.env['WOO_STORE_URL']}")
        print(f"verify_ssl: {client.verify_ssl}")
        orders = fetch_orders(client)
        with_refunds = report_embedded(orders)
        if not with_refunds:
            print("FAIL: no order carries a refunds array, nothing to probe")
            return 1
        probe_details(client, with_refunds, args.dump)
    except WooError as error:
        print(f"FAIL: {error}")
        return 1

    print(SEPARATOR)
    print("OK: probe finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
