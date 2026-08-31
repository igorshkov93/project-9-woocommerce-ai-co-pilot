"""
cleanup_orphan_orders.py

Step 15 remediation - the first topup_orders.py --apply run created 3 real
orders (today-A, today-B, today-C) before crashing on today-C's refund
(500 "payment gateway does not exist" - see probe_refund_error.py). Because
topup_orders.py is explicitly non-idempotent (run once, deliberately),
simply re-running --apply after fixing the refund bug would duplicate
those 3 orders instead of completing the plan.

This script finds and (with --apply) deletes those 3 orphan orders by
matching recent orders against today-A/today-B/today-C's known line-item
signatures from topup_orders.build_order_plan(), so the corrected
topup_orders.py --apply run can start from a clean slate.

Without --apply: prints matched candidate orders and does nothing.
With --apply: permanently deletes (force=true, bypasses trash) each
matched order.

Usage (from scripts/):
    cd scripts
    python cleanup_orphan_orders.py
    python cleanup_orphan_orders.py --apply
    cd ..
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from topup_orders import CATALOG, build_order_plan

# Only today-A/B/C could possibly have been created by the failed run -
# it crashed on today-C's refund, before today-D/yesterday-E/yesterday-F
# were ever attempted.
TARGET_LABELS = {"today-A", "today-B", "today-C"}

# Window of order IDs to scan. Order 3160 (today-C) is known from the
# earlier traceback; the other two are almost certainly 3158-3159 in a
# dev store with no concurrent traffic, but we scan a wider window and
# match by line-item signature rather than assume exact IDs.
SCAN_ID_RANGE = range(3150, 3166)


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return value


def get_verify_tls() -> bool:
    raw = os.environ.get("WOO_VERIFY_TLS", "false")
    return raw.strip().lower() not in ("false", "0", "no")


def signature_for_plan_item(plan_item: dict[str, Any]) -> set[tuple[int, int]]:
    return {(CATALOG[sku], qty) for sku, qty in plan_item["items"]}


def signature_for_order(order: dict[str, Any]) -> set[tuple[int, int]]:
    return {(li["product_id"], li["quantity"]) for li in order["line_items"]}


def fetch_order(
    base_url: str, key: str, secret: str, verify_ssl: bool, order_id: int
) -> dict[str, Any] | None:
    resp = requests.get(
        f"{base_url}/wp-json/wc/v3/orders/{order_id}",
        auth=(key, secret),
        verify=verify_ssl,
        timeout=30,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def delete_order(
    base_url: str, key: str, secret: str, verify_ssl: bool, order_id: int
) -> dict[str, Any]:
    resp = requests.delete(
        f"{base_url}/wp-json/wc/v3/orders/{order_id}",
        params={"force": "true"},
        auth=(key, secret),
        verify=verify_ssl,
        timeout=30,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    base_url = get_env("WOO_STORE_URL").rstrip("/")
    key = get_env("WOO_CONSUMER_KEY")
    secret = get_env("WOO_CONSUMER_SECRET")
    verify_ssl = get_verify_tls()
    tz = ZoneInfo(os.environ.get("BUSINESS_TIMEZONE", "Europe/Kyiv"))

    plan = build_order_plan(datetime.now(tz))
    target_plan = [p for p in plan if p["label"] in TARGET_LABELS]
    target_signatures: dict[str, set[tuple[int, int]]] = {
        p["label"]: signature_for_plan_item(p) for p in target_plan
    }

    matches: list[dict[str, Any]] = []
    for order_id in SCAN_ID_RANGE:
        order = fetch_order(base_url, key, secret, verify_ssl, order_id)
        if order is None or order["status"] == "trash":
            continue
        order_sig = signature_for_order(order)
        for label, sig in target_signatures.items():
            if order_sig == sig:
                matches.append(
                    {
                        "label": label,
                        "order_id": order["id"],
                        "status": order["status"],
                        "date_created": order["date_created"],
                    }
                )
                break

    if not args.apply:
        print(
            json.dumps(
                {"status": "dry_run", "matches": matches}, indent=2, ensure_ascii=False
            )
        )
        return

    deleted = []
    for match in matches:
        delete_order(base_url, key, secret, verify_ssl, match["order_id"])
        deleted.append(match)

    print(
        json.dumps(
            {"status": "deleted", "orders": deleted}, indent=2, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
