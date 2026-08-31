"""
create_coupon.py

Step 13, sub-step 13.3 - reference implementation for the create_coupon
tool ("Создай новый купон -15% на категорию Электроника").

Implements both contract phases from ADR 0006 under one CLI:
- default (no --apply): "propose" phase - resolves the category and its
  descendants, builds the full coupon payload, checks for an existing
  duplicate, but never calls POST. Prints status "proposed".
- --apply: "confirm" phase - re-checks for a duplicate (idempotency guard)
  and, if none exists, creates the coupon. Prints status "created" or
  "duplicate".

Contract: proposed | created | duplicate | invalid_input | no_data | upstream_error
Exit code: 0 for proposed/created/duplicate, 1 for invalid_input/no_data/upstream_error.

Usage (from scripts/):
    cd scripts
    python create_coupon.py --category-id 67 --discount-percent 15
    python create_coupon.py --category-id 67 --discount-percent 15 --apply
    cd ..
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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


def emit(status: str, **extra: Any) -> None:
    print(json.dumps({"status": status, **extra}, indent=2, ensure_ascii=False))


def fail(status: str, **extra: Any) -> None:
    emit(status, **extra)
    sys.exit(1)


def fetch_all_categories(
    base_url: str, key: str, secret: str, verify_ssl: bool
) -> list[dict[str, Any]]:
    categories: list[dict[str, Any]] = []
    page = 1
    while True:
        params: dict[str, str | int] = {
            "per_page": 100,
            "page": page,
            "orderby": "id",
            "order": "asc",
        }
        resp = requests.get(
            f"{base_url}/wp-json/wc/v3/products/categories",
            params=params,
            auth=(key, secret),
            verify=verify_ssl,
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        categories.extend(batch)
        page += 1
        if page > 50:
            break
    return categories


def collect_descendant_ids(
    category_id: int, children_of: dict[int, list[dict[str, Any]]]
) -> list[int]:
    ids = [category_id]
    for child in children_of.get(category_id, []):
        ids.extend(collect_descendant_ids(child["id"], children_of))
    return ids


def resolve_category(
    base_url: str, key: str, secret: str, verify_ssl: bool, category_id: int
) -> tuple[dict[str, Any], list[int]] | None:
    categories = fetch_all_categories(base_url, key, secret, verify_ssl)
    by_id = {c["id"]: c for c in categories}
    if category_id not in by_id:
        return None
    children_of: dict[int, list[dict[str, Any]]] = {}
    for cat in categories:
        children_of.setdefault(cat["parent"], []).append(cat)
    descendant_ids = collect_descendant_ids(category_id, children_of)
    return by_id[category_id], descendant_ids


def find_existing_coupon(
    base_url: str, key: str, secret: str, verify_ssl: bool, code: str
) -> dict[str, Any] | None:
    resp = requests.get(
        f"{base_url}/wp-json/wc/v3/coupons",
        params={"code": code},
        auth=(key, secret),
        verify=verify_ssl,
        timeout=30,
    )
    resp.raise_for_status()
    matches = resp.json()
    return matches[0] if matches else None


def build_coupon_code(category_slug: str, discount_percent: float, today: date) -> str:
    discount_token = f"{discount_percent:g}".replace(".", "_")
    return f"{category_slug.upper()}-{discount_token}-{today:%Y%m%d}"


def build_payload(
    code: str,
    discount_percent: float,
    category_ids: list[int],
    expires_on: date,
) -> dict[str, Any]:
    return {
        "code": code,
        "discount_type": "percent",
        "amount": f"{discount_percent:.2f}",
        "date_expires": expires_on.isoformat(),
        "individual_use": True,
        "exclude_sale_items": True,
        "usage_limit_per_user": 1,
        "product_categories": category_ids,
    }


def create_coupon(
    base_url: str, key: str, secret: str, verify_ssl: bool, payload: dict[str, Any]
) -> dict[str, Any]:
    resp = requests.post(
        f"{base_url}/wp-json/wc/v3/coupons",
        json=payload,
        auth=(key, secret),
        verify=verify_ssl,
        timeout=30,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category-id", type=int, required=True)
    parser.add_argument("--discount-percent", type=float, required=True)
    parser.add_argument("--expires-days", type=int, default=14)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not (0 < args.discount_percent <= 100):
        fail("invalid_input", message="discount_percent must be in (0, 100]")
    if args.expires_days < 1:
        fail("invalid_input", message="expires_days must be >= 1")

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    base_url = get_env("WOO_STORE_URL").rstrip("/")
    key = get_env("WOO_CONSUMER_KEY")
    secret = get_env("WOO_CONSUMER_SECRET")
    verify_ssl = get_verify_tls()
    tz = ZoneInfo(os.environ.get("BUSINESS_TIMEZONE", "Europe/Kyiv"))

    try:
        resolved = resolve_category(base_url, key, secret, verify_ssl, args.category_id)
    except requests.RequestException as exc:
        fail("upstream_error", message=str(exc))
        return

    if resolved is None:
        fail("no_data", message=f"category id {args.category_id} not found")
        return
    category, descendant_ids = resolved

    today = datetime.now(tz).date()
    expires_on = today + timedelta(days=args.expires_days)
    code = build_coupon_code(category["slug"], args.discount_percent, today)
    payload = build_payload(code, args.discount_percent, descendant_ids, expires_on)

    try:
        existing = find_existing_coupon(base_url, key, secret, verify_ssl, code)
    except requests.RequestException as exc:
        fail("upstream_error", message=str(exc))
        return

    if not args.apply:
        emit(
            "proposed",
            category={
                "id": category["id"],
                "slug": category["slug"],
                "name": category["name"],
            },
            descendant_category_ids=descendant_ids,
            would_be_duplicate=existing is not None,
            payload=payload,
        )
        return

    if existing is not None:
        emit("duplicate", coupon_id=existing["id"], code=existing["code"])
        return

    try:
        created = create_coupon(base_url, key, secret, verify_ssl, payload)
    except requests.RequestException as exc:
        fail("upstream_error", message=str(exc))
        return

    emit("created", coupon_id=created["id"], code=created["code"], payload=payload)


if __name__ == "__main__":
    main()
