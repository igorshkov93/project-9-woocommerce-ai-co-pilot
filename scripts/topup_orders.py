"""
topup_orders.py

Step 15 prerequisite - the demo store's newest order is 2026-08-21, which
makes "today"-windowed tools (get_orders_summary, and the Slack digest on
Step 17) return no_data. This script creates a handful of new orders
today and yesterday (plus one partial refund) so those tools have live
data again.

WooCommerce REST ignores date_created on write, and this store runs HPOS
(High-Performance Order Storage), so backdating cannot be done with a raw
SQL UPDATE against wp_posts - it must go through WC_Order::set_date_created()
+ save(), which works correctly under HPOS. That requires the WordPress
CRUD layer, which is only reachable via `wp eval-file` in the LocalWP Site
Shell, not from this Python/PowerShell environment. So this script works
in two stages:

1. --apply: creates the orders (and one refund) via WooCommerce REST -
   they land with "now" timestamps - then writes a PHP file with the exact
   wp eval-file commands needed to backdate just those new order IDs.
2. You run that PHP file with `wp eval-file <path>` from the Site Shell
   (LocalWP, cmd, & separator) to actually apply the backdating and flush
   the cache.

Refunds are created with api_refund=False: WooCommerce's REST API defaults
to attempting the refund through the order's payment gateway, but orders
created via REST (set_paid=True, no real payment_method) aren't attached
to any actual gateway - that produces a 500
"woocommerce_rest_cannot_create_order_refund" / "The payment gateway for
this order does not exist." api_refund=False just records the refund
without trying to call a gateway, which is exactly right for seeded demo
data.

Order-creating and refund-creating requests use a 60s timeout (rather than
this project's usual 30s default) - WooCommerce order creation can
synchronously trigger hooks (e.g. new-order email via the local SMTP
catcher) that occasionally run slow on LocalWP, and a hung HTTP response
here is worse than a slightly longer wait: it risks leaving a real,
already-created order stranded as an orphan (as happened on the first
--apply attempt).

Without --apply: prints the order plan (products, quantities, target
dates) and creates nothing - review before spending real WooCommerce
writes.

Re-running with --apply creates NEW orders on top of any previous run -
this is not idempotent by design (a one-off seeding action, not a tool
with a duplicate-detection contract like create_coupon). Run it once,
deliberately.

Usage (from scripts/):
    cd scripts
    python topup_orders.py
    python topup_orders.py --apply
    cd ..
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

# product_id/sku taken from prior probes (Step 10 catalog, Step 14
# abandoned-carts probe) - no new probe needed, these are already known.
CATALOG = {
    "ELEC-EARBUD-01": 2864,
    "ELEC-WATCH-01": 2865,
    "ELEC-SPEAKER-01": 2866,
    "ELEC-POWER-01": 2867,
    "ELEC-MOUSE-01": 2869,
    "woo-beanie": 2821,
    "woo-cap": 2823,
}


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return value


def get_verify_tls() -> bool:
    raw = os.environ.get("WOO_VERIFY_TLS", "false")
    return raw.strip().lower() not in ("false", "0", "no")


def build_order_plan(today: datetime) -> list[dict[str, Any]]:
    yesterday = today - timedelta(days=1)
    return [
        {
            "label": "today-A",
            "target_datetime": today.replace(
                hour=10, minute=15, second=0, microsecond=0
            ),
            "status": "completed",
            "items": [("ELEC-EARBUD-01", 1), ("woo-beanie", 1)],
            "refund_sku": None,
        },
        {
            "label": "today-B",
            "target_datetime": today.replace(
                hour=12, minute=40, second=0, microsecond=0
            ),
            "status": "processing",
            "items": [("ELEC-MOUSE-01", 2)],
            "refund_sku": None,
        },
        {
            "label": "today-C",
            "target_datetime": today.replace(
                hour=14, minute=5, second=0, microsecond=0
            ),
            "status": "completed",
            "items": [("ELEC-WATCH-01", 1), ("ELEC-EARBUD-01", 1)],
            "refund_sku": "ELEC-EARBUD-01",
        },
        {
            "label": "today-D",
            "target_datetime": today.replace(
                hour=16, minute=30, second=0, microsecond=0
            ),
            "status": "processing",
            "items": [("woo-cap", 1), ("ELEC-POWER-01", 2)],
            "refund_sku": None,
        },
        {
            "label": "yesterday-E",
            "target_datetime": yesterday.replace(
                hour=11, minute=0, second=0, microsecond=0
            ),
            "status": "completed",
            "items": [("ELEC-SPEAKER-01", 1)],
            "refund_sku": None,
        },
        {
            "label": "yesterday-F",
            "target_datetime": yesterday.replace(
                hour=15, minute=20, second=0, microsecond=0
            ),
            "status": "processing",
            "items": [("woo-beanie", 1), ("woo-cap", 1)],
            "refund_sku": None,
        },
    ]


def create_order(
    base_url: str, key: str, secret: str, verify_ssl: bool, plan_item: dict[str, Any]
) -> dict[str, Any]:
    line_items = [
        {"product_id": CATALOG[sku], "quantity": qty} for sku, qty in plan_item["items"]
    ]
    payload = {
        "status": plan_item["status"],
        "line_items": line_items,
        "set_paid": True,
    }
    resp = requests.post(
        f"{base_url}/wp-json/wc/v3/orders",
        json=payload,
        auth=(key, secret),
        verify=verify_ssl,
        timeout=60,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def refund_line_item(
    base_url: str,
    key: str,
    secret: str,
    verify_ssl: bool,
    order: dict[str, Any],
    sku: str,
    label: str,
) -> dict[str, Any]:
    target_line = next(li for li in order["line_items"] if li["sku"] == sku)
    payload = {
        "reason": f"Demo refund seeded by topup_orders.py ({label})",
        "api_refund": False,
        "line_items": [
            {
                "id": target_line["id"],
                "quantity": target_line["quantity"],
                "refund_total": target_line["total"],
            }
        ],
    }
    resp = requests.post(
        f"{base_url}/wp-json/wc/v3/orders/{order['id']}/refunds",
        json=payload,
        auth=(key, secret),
        verify=verify_ssl,
        timeout=60,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def build_backdate_php(order_dates: dict[int, str]) -> str:
    entries = ",\n".join(
        f"    {order_id} => '{date_str}'" for order_id, date_str in order_dates.items()
    )
    return f"""<?php
$targets = [
{entries},
];

foreach ( $targets as $order_id => $date_string ) {{
    $order = wc_get_order( $order_id );
    if ( ! $order ) {{
        WP_CLI::warning( "Order {{$order_id}} not found, skipping" );
        continue;
    }}
    $order->set_date_created( $date_string );
    $order->set_date_modified( $date_string );
    $order->save();
    WP_CLI::success( "Order {{$order_id}} backdated to {{$date_string}}" );
}}

wp_cache_flush();
WP_CLI::success( 'Cache flushed' );
"""


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

    today = datetime.now(tz)
    plan = build_order_plan(today)

    if not args.apply:
        preview = [
            {
                "label": p["label"],
                "target_datetime": p["target_datetime"].isoformat(),
                "status": p["status"],
                "items": p["items"],
                "refund_sku": p["refund_sku"],
            }
            for p in plan
        ]
        print(
            json.dumps(
                {"status": "dry_run", "plan": preview}, indent=2, ensure_ascii=False
            )
        )
        return

    order_dates: dict[int, str] = {}
    created_summary: list[dict[str, Any]] = []

    for item in plan:
        order = create_order(base_url, key, secret, verify_ssl, item)
        order_dates[order["id"]] = item["target_datetime"].strftime("%Y-%m-%d %H:%M:%S")
        created_summary.append(
            {"label": item["label"], "order_id": order["id"], "status": order["status"]}
        )

        if item["refund_sku"]:
            refund = refund_line_item(
                base_url,
                key,
                secret,
                verify_ssl,
                order,
                item["refund_sku"],
                item["label"],
            )
            created_summary[-1]["refund_id"] = refund["id"]

    php_content = build_backdate_php(order_dates)
    php_path = os.path.join(os.path.dirname(__file__), "topup_backdate.php")
    with open(php_path, "w", encoding="utf-8") as f:
        f.write(php_content)

    print(
        json.dumps(
            {
                "status": "created",
                "orders": created_summary,
                "backdate_script": php_path,
                "next_step": "run `wp eval-file <path shown above>` from the LocalWP Site Shell, then verify",
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
