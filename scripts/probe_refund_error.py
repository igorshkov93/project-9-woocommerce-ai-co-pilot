"""
probe_refund_error.py

Diagnostic for the 500 Internal Server Error hit by topup_orders.py when
attempting to refund order 3160's ELEC-EARBUD-01 line item. WooCommerce's
REST API returns a JSON error body on failure, but topup_orders.py's
refund_line_item() calls raise_for_status() immediately, discarding that
body and leaving only the bare "500 Internal Server Error" from requests.

This script re-issues the exact same refund request and prints the full
response body (status + JSON/text) regardless of outcome, read-only aside
from the refund attempt itself - if WooCommerce actually processes it
this time, that IS the fix we needed (Step 15 continues); if it fails
again, we get the real error message to diagnose from.

Usage (from scripts/):
    cd scripts
    python probe_refund_error.py --order-id 3160 --sku ELEC-EARBUD-01
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", type=int, required=True)
    parser.add_argument("--sku", type=str, required=True)
    args = parser.parse_args()

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    base_url = get_env("WOO_STORE_URL").rstrip("/")
    key = get_env("WOO_CONSUMER_KEY")
    secret = get_env("WOO_CONSUMER_SECRET")
    verify_ssl = get_verify_tls()

    order_resp = requests.get(
        f"{base_url}/wp-json/wc/v3/orders/{args.order_id}",
        auth=(key, secret),
        verify=verify_ssl,
        timeout=30,
    )
    order_resp.raise_for_status()
    order: dict[str, Any] = order_resp.json()

    target_line = next(li for li in order["line_items"] if li["sku"] == args.sku)
    print("Target line item:", json.dumps(target_line, indent=2, ensure_ascii=False))

    payload = {
        "reason": "Demo refund seeded by topup_orders.py (probe)",
        "line_items": [
            {
                "id": target_line["id"],
                "quantity": target_line["quantity"],
                "refund_total": target_line["total"],
            }
        ],
    }
    print("Refund payload:", json.dumps(payload, indent=2, ensure_ascii=False))

    resp = requests.post(
        f"{base_url}/wp-json/wc/v3/orders/{args.order_id}/refunds",
        json=payload,
        auth=(key, secret),
        verify=verify_ssl,
        timeout=30,
    )

    print(f"Status: {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(resp.text)


if __name__ == "__main__":
    main()
