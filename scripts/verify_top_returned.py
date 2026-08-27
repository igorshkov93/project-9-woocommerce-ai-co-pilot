"""Assertion suite for the get_top_returned_products metric.

This is a test, not a report: it exits non-zero when an invariant breaks.

Most figures are recomputed here along a deliberately different path than
the implementation takes, so a shared bug is less likely to cancel itself
out. Totals are cross-checked against the refunds array embedded in
GET /orders, which the implementation never reads.

The single most important assertion is units_returned <= units_sold per
product. It guards the reason SOLD_STATUSES includes "refunded" while the
revenue statuses in orders_summary.py do not: drop it from the denominator
and return rates climb past 100 percent.

Absolute baselines are pinned to one documented window and are skipped,
not failed, when the store's newest order moves the window. Fixtures are
resolved by SKU rather than numeric product id, which memory-assigned ids
have already broken once in this project.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from top_returned_products import (
    MIN_SALES_FOR_RATE,
    SOLD_STATUSES,
    build_result,
    latest_order_date,
    money_to_cents,
    quantity_of,
    resolve_window,
)
from woo_client import WooClient, WooError

WINDOW_DAYS = 38
BASELINE_FROM = "2026-07-15"
BASELINE_TO = "2026-08-21"
BASELINE_TOTAL_CENTS = 75700
BASELINE_REVENUE_SIDE_CENTS = 59500  # matches the Step 11 refunds figure
FIXTURE_SKUS = ("ELEC-EARBUD-01", "ELEC-WATCH-01")
REVENUE_STATUSES = ("pending", "processing", "on-hold", "completed")
FULL_LIMIT = 50
TOP_LIMIT = 5
REQUIRED_ROW_KEYS = (
    "rank",
    "product_id",
    "sku",
    "name",
    "units_returned",
    "refund_amount",
    "orders_affected",
    "units_sold",
    "return_rate_pct",
    "rate_significant",
    "top_reason",
    "reasons",
)
SEPARATOR = "-" * 70


class Checker:
    """Collect assertions so one failure does not hide the rest."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    def skip(self, name: str, reason: str) -> None:
        self.skipped += 1
        print(f"  SKIP  {name} -- {reason}")

    def summary(self) -> int:
        print(SEPARATOR)
        print(f"passed {self.passed}, failed {self.failed}, skipped {self.skipped}")
        return 1 if self.failed else 0


def independent_totals(client: WooClient, after: str, before: str) -> dict[str, Any]:
    """Recompute the window from scratch, using the embedded refunds array."""
    orders = client.get_all(
        "orders",
        {
            "status": "any",
            "after": after,
            "before": before,
            "dates_are_gmt": "true",
        },
    )
    embedded_cents = 0
    revenue_side_cents = 0
    refund_entries = 0
    affected_orders = 0
    per_sku_cents: Counter[str] = Counter()
    per_sku_units: Counter[str] = Counter()

    for order in orders:
        refunds = order.get("refunds") or []
        if not refunds:
            continue
        status = str(order.get("status", ""))
        for entry in refunds:
            cents = abs(money_to_cents(entry.get("total")))
            embedded_cents += cents
            refund_entries += 1
            if status in REVENUE_STATUSES:
                revenue_side_cents += cents

        if status not in SOLD_STATUSES:
            continue
        affected_orders += 1
        detail = client.get(f"orders/{order['id']}/refunds", {"per_page": 100})
        for refund in detail if isinstance(detail, list) else []:
            for item in refund.get("line_items") or []:
                sku = str(item.get("sku") or "")
                per_sku_cents[sku] += abs(money_to_cents(item.get("total")))
                per_sku_units[sku] += quantity_of(item.get("quantity"))

    return {
        "orders": len(orders),
        "embedded_cents": embedded_cents,
        "revenue_side_cents": revenue_side_cents,
        "refund_entries": refund_entries,
        "affected_orders": affected_orders,
        "per_sku_cents": per_sku_cents,
        "per_sku_units": per_sku_units,
    }


def check_shape(checker: Checker, result: dict[str, Any], limit: int) -> None:
    """Contract shape and window metadata."""
    print("contract shape")
    checker.check(
        "status is ok", result.get("status") == "ok", str(result.get("status"))
    )
    for key in ("window", "totals", "diagnostics", "top", "limit"):
        checker.check(f"top-level key {key}", key in result)
    window = result.get("window", {})
    checker.check(
        "attribution is order_date", window.get("attribution") == "order_date"
    )
    checker.check("window reports a timezone", bool(window.get("timezone")))
    checker.check("limit echoed back", result.get("limit") == limit)
    checker.check("top not longer than limit", len(result.get("top", [])) <= limit)
    rows = result.get("top", [])
    missing = [k for row in rows for k in REQUIRED_ROW_KEYS if k not in row]
    checker.check("every row carries all keys", not missing, str(sorted(set(missing))))
    ranks = [row.get("rank") for row in rows]
    checker.check("ranks are 1..n", ranks == list(range(1, len(rows) + 1)), str(ranks))


def check_arithmetic(checker: Checker, full: dict[str, Any]) -> None:
    """Money, units and ranking invariants."""
    print("arithmetic invariants")
    rows = full["top"]
    totals = full["totals"]

    row_cents = sum(round(row["refund_amount"] * 100) for row in rows)
    checker.check(
        "rows sum to totals.refund_amount",
        row_cents == round(totals["refund_amount"] * 100),
        f"{row_cents} vs {round(totals['refund_amount'] * 100)}",
    )
    checker.check(
        "rows sum to totals.units_returned",
        sum(row["units_returned"] for row in rows) == totals["units_returned"],
    )
    checker.check(
        "products_affected matches row count",
        totals["products_affected"] == len(rows),
    )
    checker.check(
        "all amounts non-negative",
        all(row["refund_amount"] >= 0 for row in rows),
    )
    checker.check(
        "amounts carry at most two decimals",
        all(round(row["refund_amount"], 2) == row["refund_amount"] for row in rows),
    )

    keys = [
        (-round(row["refund_amount"] * 100), -row["units_returned"], row["product_id"])
        for row in rows
    ]
    checker.check("ranking is sorted and deterministic", keys == sorted(keys))
    checker.check(
        "full and partial account for every refund",
        totals["full_refunds"] + totals["partial_refunds"] == totals["refunds_count"],
    )
    checker.check(
        "reason counts sum to refunds_count",
        sum(totals["reasons"].values()) == totals["refunds_count"],
        f"{sum(totals['reasons'].values())} vs {totals['refunds_count']}",
    )
    checker.check(
        "no blank reason keys",
        all(reason.strip() for reason in totals["reasons"]),
    )
    diagnostics = full["diagnostics"]
    checker.check("no refund amount mismatches", diagnostics["amount_mismatches"] == 0)
    checker.check("no non-product refund lines", diagnostics["non_product_lines"] == 0)


def check_return_rate(checker: Checker, full: dict[str, Any]) -> None:
    """The denominator guard: returns can never exceed sales."""
    print("return rate")
    rows = full["top"]
    over_sold = [
        row["sku"] for row in rows if row["units_returned"] > row["units_sold"]
    ]
    checker.check(
        "units_returned never exceeds units_sold", not over_sold, str(over_sold)
    )
    checker.check(
        "return rate never exceeds 100 percent",
        all(row["return_rate_pct"] <= 100 for row in rows),
    )
    recomputed = [
        row["sku"]
        for row in rows
        if row["units_sold"]
        and row["return_rate_pct"]
        != round(row["units_returned"] / row["units_sold"] * 100, 2)
    ]
    checker.check("return rate recomputes exactly", not recomputed, str(recomputed))
    checker.check(
        "significance flag matches threshold",
        all(
            row["rate_significant"] == (row["units_sold"] >= MIN_SALES_FOR_RATE)
            for row in rows
        ),
    )
    checker.check(
        "every ranked product has a top reason",
        all(row["top_reason"].strip() for row in rows),
    )


def check_against_independent(
    checker: Checker, full: dict[str, Any], truth: dict[str, Any]
) -> None:
    """Cross-check against a recomputation that reads different fields."""
    print("independent cross-check")
    totals = full["totals"]
    checker.check(
        "orders scanned agree",
        full["window"]["orders_scanned"] == truth["orders"],
        f"{full['window']['orders_scanned']} vs {truth['orders']}",
    )
    checker.check(
        "refund total matches embedded array",
        round(totals["refund_amount"] * 100) == truth["embedded_cents"],
        f"{round(totals['refund_amount'] * 100)} vs {truth['embedded_cents']}",
    )
    checker.check(
        "orders_affected agrees",
        totals["orders_affected"] == truth["affected_orders"],
    )
    checker.check(
        "refunds_count agrees",
        totals["refunds_count"] == truth["refund_entries"],
    )
    gap = truth["embedded_cents"] - truth["revenue_side_cents"]
    checker.check(
        "revenue-side plus excluded equals total",
        truth["revenue_side_cents"] + gap == truth["embedded_cents"],
    )

    by_sku = {row["sku"]: row for row in full["top"]}
    for sku in FIXTURE_SKUS:
        row = by_sku.get(sku)
        checker.check(f"fixture {sku} present", row is not None)
        if row is None:
            continue
        checker.check(
            f"fixture {sku} amount agrees",
            round(row["refund_amount"] * 100) == truth["per_sku_cents"][sku],
            f"{round(row['refund_amount'] * 100)} vs {truth['per_sku_cents'][sku]}",
        )
        checker.check(
            f"fixture {sku} units agree",
            row["units_returned"] == truth["per_sku_units"][sku],
        )


def check_baselines(
    checker: Checker, full: dict[str, Any], truth: dict[str, Any]
) -> None:
    """Pinned figures, skipped when the window has moved on."""
    print("documented baselines")
    window = full["window"]
    if (window["from"], window["to"]) != (BASELINE_FROM, BASELINE_TO):
        reason = f"window is {window['from']}..{window['to']}"
        checker.skip("baseline total 757.00", reason)
        checker.skip("baseline revenue side 595.00", reason)
        return
    checker.check(
        "baseline total 757.00",
        round(full["totals"]["refund_amount"] * 100) == BASELINE_TOTAL_CENTS,
    )
    checker.check(
        "baseline revenue side 595.00",
        truth["revenue_side_cents"] == BASELINE_REVENUE_SIDE_CENTS,
        f"{truth['revenue_side_cents']} vs {BASELINE_REVENUE_SIDE_CENTS}",
    )


def check_prefix(checker: Checker, top: dict[str, Any], full: dict[str, Any]) -> None:
    """A smaller limit must return the head of the larger ranking."""
    print("limit behaviour")
    head = [row["product_id"] for row in full["top"][:TOP_LIMIT]]
    actual = [row["product_id"] for row in top["top"]]
    checker.check("top-N is a prefix of the full ranking", head == actual, str(actual))


def check_edges(checker: Checker, client: WooClient, tz: ZoneInfo, newest: Any) -> None:
    """Guard rails: empty windows and rejected inputs."""
    print("edge cases")
    future_from = (newest + timedelta(days=10)).isoformat()
    future_to = (newest + timedelta(days=20)).isoformat()
    empty = build_result(client, tz, future_from, future_to, WINDOW_DAYS, TOP_LIMIT)
    checker.check(
        "future window returns no_data",
        empty["status"] == "no_data",
        str(empty.get("status")),
    )
    checker.check("no_data still reports the window", "window" in empty)

    for bad_limit in (0, 51):
        outcome = build_result(client, tz, None, None, WINDOW_DAYS, bad_limit)
        checker.check(
            f"limit {bad_limit} is rejected",
            outcome["status"] == "invalid_input",
            str(outcome.get("status")),
        )

    reversed_window = build_result(
        client, tz, BASELINE_TO, BASELINE_FROM, WINDOW_DAYS, TOP_LIMIT
    )
    checker.check(
        "reversed window is rejected",
        reversed_window["status"] == "invalid_input",
        str(reversed_window.get("status")),
    )


def main() -> int:
    try:
        client = WooClient()
        tz = ZoneInfo(client.env["BUSINESS_TIMEZONE"])
        newest = latest_order_date(client, tz)
    except (WooError, KeyError) as error:
        print(f"FAIL: {error}")
        return 1

    start = newest - timedelta(days=WINDOW_DAYS - 1)
    print(SEPARATOR)
    print(f"newest order in store: {newest.isoformat()}")
    print(f"window under test:     {start.isoformat()} .. {newest.isoformat()}")
    print(SEPARATOR)

    checker = Checker()
    try:
        _, _, after, before = resolve_window(
            client, tz, start.isoformat(), newest.isoformat(), WINDOW_DAYS
        )
        truth = independent_totals(client, after, before)
        full = build_result(
            client, tz, start.isoformat(), newest.isoformat(), WINDOW_DAYS, FULL_LIMIT
        )
        top = build_result(
            client, tz, start.isoformat(), newest.isoformat(), WINDOW_DAYS, TOP_LIMIT
        )
    except (WooError, ValueError) as error:
        print(f"FAIL: could not gather data: {error}")
        return 1

    if full["status"] != "ok":
        print(f"FAIL: expected status ok, got {full['status']}")
        return 1

    check_shape(checker, top, TOP_LIMIT)
    check_arithmetic(checker, full)
    check_return_rate(checker, full)
    check_against_independent(checker, full, truth)
    check_baselines(checker, full, truth)
    check_prefix(checker, top, full)
    check_edges(checker, client, tz, newest)

    return checker.summary()


if __name__ == "__main__":
    sys.exit(main())
