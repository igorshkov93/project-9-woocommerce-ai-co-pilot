"""
list_recent_orders.py

Step 15 remediation - topup_orders.py --apply timed out on its first
create_order() POST (30s read timeout, no response from the LocalWP
server). Because the script only prints progress at the very end, a
timeout on the HTTP request leaves it unknown whether WooCommerce
actually created the order server-side before the response was lost.

Read-only diagnostic: lists the most recent orders by date_created so we
can check whether an orphan order exists before deciding to retry
topup_orders.py --apply.

Usage (from scripts/):
    cd scripts
    python list_recent_orders.py
    python list_recent_orders.py --limit 10
    cd ..
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests
from dotenv import load_dotenv


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return value


def get_verify_tls() -> bool:
    raw = os.environ.get("WOO_VERIFY_TLS", "false")
    return raw.strip().lower() not in ("false", "0", "no")


def fetch_recent_orders(
    base_url: str, key: str, secret: str, verify_ssl: bool, limit: int
) -> list[dict[str, Any]]:
    params: dict[str, str | int] = {
        "orderby": "date",
        "order": "desc",
        "per_page": limit,
    }
    resp = requests.get(
        f"{base_url}/wp-json/wc/v3/orders",
        params=params,
        auth=(key, secret),
        verify=verify_ssl,
        timeout=60,
    )
    resp.raise_for_status()
    result: list[dict[str, Any]] = resp.json()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    base_url = get_env("WOO_STORE_URL").rstrip("/")
    key = get_env("WOO_CONSUMER_KEY")
    secret = get_env("WOO_CONSUMER_SECRET")
    verify_ssl = get_verify_tls()

    orders = fetch_recent_orders(base_url, key, secret, verify_ssl, args.limit)

    summary = [
        {
            "id": order["id"],
            "status": order["status"],
            "date_created": order["date_created"],
            "items": [
                {"sku": li["sku"], "quantity": li["quantity"]}
                for li in order["line_items"]
            ],
        }
        for order in orders
    ]
    print(json.dumps({"status": "ok", "orders": summary}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
