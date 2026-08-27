"""Reference implementation of the get_top_returned_products metric.

This is the source of truth the n8n workflow is verified against, so every
arithmetic decision here is deliberate.

Money is accumulated as integer cents. The n8n side computes the same
figures in JavaScript, where Decimal does not exist and float accumulation
drifts differently than in Python. Integer cents reproduce exactly in both
runtimes, which is what makes a to-the-cent comparison meaningful rather
than lucky.

Refund line items are the only reliable source of "which product came
back": the refunds array embedded in GET /orders carries no line_items, so
a detail call per refunded order is unavoidable. That call is issued only
for orders whose embedded array is non-empty, which binds N to the number
of refunds rather than the number of orders.

Refunds are attributed to the window by the date of the parent order, not
the date of the refund. The seeded refunds all share a single creation
date, so refund-date attribution would return either everything or
nothing. The mode is explicit and switchable rather than hardcoded.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from woo_client import WooClient, WooError

# Statuses that count as a sale for the return-rate denominator.
# This deliberately differs from REVENUE_STATUSES in orders_summary.py.
# A fully refunded order is excluded from revenue because the money left,
# but the unit was still sold before it came back. Dropping it from the
# denominator would let return_rate exceed 100 percent.
SOLD_STATUSES = ("pending", "processing", "on-hold", "completed", "refunded")

MIN_SALES_FOR_RATE = 3
DEFAULT_LIMIT = 5
DEFAULT_DAYS = 30
UNSPECIFIED_REASON = "unspecified"
ATTRIBUTION_MODE = "order_date"
SEPARATOR = "-" * 72


def money_to_cents(value: Any) -> int:
    """Convert a WooCommerce money string to integer cents."""
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return 0


def cents_to_amount(cents: int) -> float:
    """Render integer cents as a two-decimal number for the contract."""
    return round(cents / 100.0, 2)


def quantity_of(value: Any) -> int:
    """Refund quantities are stored negative; return the absolute units."""
    try:
        return abs(int(float(value)))
    except (TypeError, ValueError):
        return 0


def normalise_reason(value: Any) -> str:
    """Collapse missing, null and blank reasons into one bucket."""
    if not isinstance(value, str) or not value.strip():
        return UNSPECIFIED_REASON
    return value.strip()


def refund_type_of(refund: dict[str, Any]) -> str:
    """Read _refund_type from meta_data: full, partial or unknown."""
    for meta in refund.get("meta_data") or []:
        if isinstance(meta, dict) and meta.get("key") == "_refund_type":
            return str(meta.get("value") or "unknown")
    return "unknown"


def latest_order_date(client: WooClient, business_tz: ZoneInfo) -> date:
    """Find the newest order date so windows anchor to real data."""
    orders = client.get(
        "orders",
        {"status": "any", "per_page": 1, "orderby": "date", "order": "desc"},
    )
    if not orders:
        raise WooError("store has no orders at all")
    raw = str(orders[0].get("date_created_gmt", ""))
    stamp = datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo("UTC"))
    return stamp.astimezone(business_tz).date()


def resolve_window(
    client: WooClient,
    business_tz: ZoneInfo,
    from_arg: str | None,
    to_arg: str | None,
    days: int,
) -> tuple[date, date, str, str]:
    """Turn user input into local dates plus exclusive UTC API bounds."""
    end_date = (
        datetime.strptime(to_arg, "%Y-%m-%d").date()
        if to_arg
        else latest_order_date(client, business_tz)
    )
    start_date = (
        datetime.strptime(from_arg, "%Y-%m-%d").date()
        if from_arg
        else end_date - timedelta(days=days - 1)
    )
    if start_date > end_date:
        raise ValueError("from date is later than to date")

    start_local = datetime.combine(start_date, datetime.min.time(), tzinfo=business_tz)
    end_local = datetime.combine(
        end_date + timedelta(days=1), datetime.min.time(), tzinfo=business_tz
    )
    utc = ZoneInfo("UTC")
    # after and before are strictly exclusive: nudge the lower bound back by
    # one second so an order landing exactly on midnight is not dropped.
    after = (start_local.astimezone(utc) - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    before = end_local.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%S")
    return start_date, end_date, after, before


def fetch_orders(client: WooClient, after: str, before: str) -> list[dict[str, Any]]:
    """Fetch every order whose creation date falls inside the window."""
    params = {
        "status": "any",
        "after": after,
        "before": before,
        "dates_are_gmt": "true",
        "orderby": "date",
        "order": "asc",
    }
    orders = client.get_all("orders", params)
    return [order for order in orders if isinstance(order, dict)]


def count_units_sold(orders: list[dict[str, Any]]) -> Counter[int]:
    """Count units sold per product, the denominator of the return rate."""
    sold: Counter[int] = Counter()
    for order in orders:
        if order.get("status") not in SOLD_STATUSES:
            continue
        for item in order.get("line_items") or []:
            product_id = int(item.get("product_id") or 0)
            if product_id:
                sold[product_id] += quantity_of(item.get("quantity"))
    return sold


def collect_refunds(
    client: WooClient, orders: list[dict[str, Any]]
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Aggregate refund line items per product via the detail endpoint."""
    products: dict[int, dict[str, Any]] = {}
    stats: dict[str, Any] = {
        "refunds_count": 0,
        "orders_affected": 0,
        "refund_cents": 0,
        "units_returned": 0,
        "full_refunds": 0,
        "partial_refunds": 0,
        "detail_calls": 0,
        "excluded_by_status": 0,
        "non_product_lines": 0,
        "amount_mismatches": 0,
    }
    reasons_global: Counter[str] = Counter()

    for order in orders:
        if not order.get("refunds"):
            continue
        if order.get("status") not in SOLD_STATUSES:
            stats["excluded_by_status"] += 1
            continue

        order_id = int(order["id"])
        stats["detail_calls"] += 1
        refunds = client.get(f"orders/{order_id}/refunds", {"per_page": 100})
        if not isinstance(refunds, list) or not refunds:
            continue
        stats["orders_affected"] += 1

        for refund in refunds:
            stats["refunds_count"] += 1
            reason = normalise_reason(refund.get("reason"))
            reasons_global[reason] += 1
            kind = refund_type_of(refund)
            if kind == "full":
                stats["full_refunds"] += 1
            elif kind == "partial":
                stats["partial_refunds"] += 1

            # The detail endpoint names the total "amount"; the embedded
            # array in GET /orders names the same value "total".
            declared = abs(money_to_cents(refund.get("amount")))
            line_sum = 0

            for item in refund.get("line_items") or []:
                cents = abs(money_to_cents(item.get("total")))
                units = quantity_of(item.get("quantity"))
                line_sum += cents
                product_id = int(item.get("product_id") or 0)
                if not product_id:
                    stats["non_product_lines"] += 1
                    continue

                entry = products.setdefault(
                    product_id,
                    {
                        "product_id": product_id,
                        "sku": str(item.get("sku") or ""),
                        "name": str(item.get("name") or ""),
                        "units_returned": 0,
                        "refund_cents": 0,
                        "order_ids": set(),
                        "reasons": Counter(),
                    },
                )
                entry["units_returned"] += units
                entry["refund_cents"] += cents
                entry["order_ids"].add(order_id)
                entry["reasons"][reason] += 1
                stats["units_returned"] += units

            stats["refund_cents"] += line_sum
            if declared and abs(declared - line_sum) > 1:
                stats["amount_mismatches"] += 1

    stats["reasons"] = dict(reasons_global.most_common())
    return products, stats


def rank_products(
    products: dict[int, dict[str, Any]], sold: Counter[int], limit: int
) -> list[dict[str, Any]]:
    """Order products by refunded money, with deterministic tie-breaking."""
    rows: list[dict[str, Any]] = []
    for entry in products.values():
        product_id = int(entry["product_id"])
        units_sold = int(sold.get(product_id, 0))
        units_returned = int(entry["units_returned"])
        rate = round(units_returned / units_sold * 100, 2) if units_sold else 0.0
        reasons: Counter[str] = entry["reasons"]
        top_reason = reasons.most_common(1)[0][0] if reasons else UNSPECIFIED_REASON
        rows.append(
            {
                "product_id": product_id,
                "sku": entry["sku"],
                "name": entry["name"],
                "units_returned": units_returned,
                "refund_amount": cents_to_amount(int(entry["refund_cents"])),
                "orders_affected": len(entry["order_ids"]),
                "units_sold": units_sold,
                "return_rate_pct": rate,
                "rate_significant": units_sold >= MIN_SALES_FOR_RATE,
                "top_reason": top_reason,
                "reasons": dict(reasons.most_common()),
            }
        )

    # Money first: on a small refund set the unit counter produces ties
    # almost everywhere, which would make the ranking arbitrary.
    rows.sort(
        key=lambda row: (
            -round(row["refund_amount"] * 100),
            -row["units_returned"],
            row["product_id"],
        )
    )
    for position, row in enumerate(rows[:limit], start=1):
        row["rank"] = position
    return rows[:limit]


def build_result(
    client: WooClient,
    business_tz: ZoneInfo,
    from_arg: str | None,
    to_arg: str | None,
    days: int,
    limit: int,
) -> dict[str, Any]:
    """Produce the tool contract: ok | no_data | invalid_input | upstream_error."""
    if limit < 1 or limit > 50:
        return {"status": "invalid_input", "error": "limit must be 1..50"}

    try:
        start_date, end_date, after, before = resolve_window(
            client, business_tz, from_arg, to_arg, days
        )
    except ValueError as error:
        return {"status": "invalid_input", "error": str(error)}
    except WooError as error:
        return {"status": "upstream_error", "error": str(error)}

    try:
        orders = fetch_orders(client, after, before)
        sold = count_units_sold(orders)
        products, stats = collect_refunds(client, orders)
    except WooError as error:
        return {"status": "upstream_error", "error": str(error)}

    window = {
        "from": start_date.isoformat(),
        "to": end_date.isoformat(),
        "timezone": str(business_tz),
        "attribution": ATTRIBUTION_MODE,
        "orders_scanned": len(orders),
    }

    if not products:
        return {"status": "no_data", "window": window, "limit": limit}

    reasons = stats.pop("reasons")
    return {
        "status": "ok",
        "window": window,
        "totals": {
            "refunds_count": stats["refunds_count"],
            "orders_affected": stats["orders_affected"],
            "refund_amount": cents_to_amount(int(stats["refund_cents"])),
            "units_returned": stats["units_returned"],
            "full_refunds": stats["full_refunds"],
            "partial_refunds": stats["partial_refunds"],
            "products_affected": len(products),
            "reasons": reasons,
        },
        "diagnostics": {
            "detail_calls": stats["detail_calls"],
            "excluded_by_status": stats["excluded_by_status"],
            "non_product_lines": stats["non_product_lines"],
            "amount_mismatches": stats["amount_mismatches"],
        },
        "top": rank_products(products, sold, limit),
        "limit": limit,
    }


def render(result: dict[str, Any]) -> None:
    """Print a human-readable summary of the contract."""
    status = result["status"]
    print(SEPARATOR)
    print(f"status: {status}")
    if status in ("invalid_input", "upstream_error"):
        print(f"error:  {result.get('error')}")
        return

    window = result["window"]
    print(
        f"window: {window['from']} .. {window['to']} "
        f"({window['timezone']}, by {window['attribution']})"
    )
    print(f"orders scanned: {window['orders_scanned']}")
    if status == "no_data":
        print("no refunds attributed to this window")
        return

    totals = result["totals"]
    diag = result["diagnostics"]
    print(SEPARATOR)
    print(
        f"refunds {totals['refunds_count']} "
        f"(full {totals['full_refunds']} / partial {totals['partial_refunds']}) "
        f"across {totals['orders_affected']} orders"
    )
    print(
        f"units returned {totals['units_returned']}, "
        f"amount {totals['refund_amount']:.2f}, "
        f"products affected {totals['products_affected']}"
    )
    print(
        f"detail calls {diag['detail_calls']}, "
        f"amount mismatches {diag['amount_mismatches']}, "
        f"non-product lines {diag['non_product_lines']}"
    )
    print(SEPARATOR)
    header = f"{'#':<3}{'SKU':<18}{'units':>6}{'amount':>10}{'sold':>6}{'rate':>8}"
    print(header)
    for row in result["top"]:
        rate = f"{row['return_rate_pct']:.1f}%"
        if not row["rate_significant"]:
            rate += "*"
        print(
            f"{row['rank']:<3}{row['sku'][:17]:<18}"
            f"{row['units_returned']:>6}{row['refund_amount']:>10.2f}"
            f"{row['units_sold']:>6}{rate:>8}"
        )
        print(f"   {row['name'][:44]}")
        print(f"   reason: {row['top_reason'][:56]}")
    print(f"* return rate not significant, fewer than {MIN_SALES_FOR_RATE} sold")
    print(SEPARATOR)
    print("global reasons:")
    for reason, count in totals["reasons"].items():
        print(f"  {count:>3}  {reason}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Top returned products report")
    parser.add_argument("--from", dest="from_date", help="YYYY-MM-DD, inclusive")
    parser.add_argument("--to", dest="to_date", help="YYYY-MM-DD, inclusive")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"window length when --from is omitted (default: {DEFAULT_DAYS})",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--json", action="store_true", help="print the raw contract instead"
    )
    args = parser.parse_args()

    try:
        client = WooClient()
        business_tz = ZoneInfo(client.env["BUSINESS_TIMEZONE"])
    except (WooError, KeyError) as error:
        print(f"FAIL: {error}")
        return 1

    result = build_result(
        client,
        business_tz,
        args.from_date,
        args.to_date,
        args.days,
        args.limit,
    )

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        render(result)

    return 0 if result["status"] in ("ok", "no_data") else 1


if __name__ == "__main__":
    sys.exit(main())
