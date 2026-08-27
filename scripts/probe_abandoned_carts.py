"""
probe_abandoned_carts.py

Step 14, sub-step 14.1 - probe the copilot-abandoned-carts REST route
(mu-plugin from Step 8: GET /wp-json/copilot/v1/abandoned-carts) to see
the actual response shape before designing draft_abandoned_cart_email.py.
Nothing about this endpoint's field names is known from this session's
own history beyond "it exists" - this probe establishes ground truth.

Read-only.

Usage (from scripts/):
    cd scripts
    python probe_abandoned_carts.py
    cd ..
"""

from __future__ import annotations

import json
import os
import sys

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


def site_root_from_copilot_base(copilot_api_base: str) -> str:
    marker = "/wp-json"
    idx = copilot_api_base.find(marker)
    if idx == -1:
        raise RuntimeError(
            f"COPILOT_API_BASE does not contain {marker!r}: {copilot_api_base!r}"
        )
    return copilot_api_base[:idx]


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

    copilot_api_base = get_env("COPILOT_API_BASE")
    wp_user = get_env("WP_APP_USER")
    wp_password = get_env("WP_APP_PASSWORD")
    verify_ssl = get_verify_tls()

    site_root = site_root_from_copilot_base(copilot_api_base)
    url = f"{site_root}/wp-json/copilot/v1/abandoned-carts"

    resp = requests.get(url, auth=(wp_user, wp_password), verify=verify_ssl, timeout=30)
    print(f"URL: {url}")
    print(f"Status: {resp.status_code}")
    resp.raise_for_status()

    data = resp.json()
    print(f"Top-level type: {type(data).__name__}")

    if isinstance(data, list):
        print(f"Item count: {len(data)}")
        if data and isinstance(data[0], dict):
            print("First item keys:", sorted(data[0].keys()))
    elif isinstance(data, dict):
        print("Top-level keys:", sorted(data.keys()))

    print("\nFull response (pretty):")
    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
