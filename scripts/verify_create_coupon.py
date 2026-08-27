"""
verify_create_coupon.py

Step 13, sub-step 13.4 - verification suite for create_coupon.py.

Exercises three layers:
1. Pure logic: build_coupon_code / build_payload against fixed inputs.
2. Live category resolution: Electronics (flat) and Clothing (nested,
   confirmed 32-descendant tree in probe_category_children.py) resolved
   by slug, not hardcoded numeric id, per project convention.
3. Live round-trip against a low-stakes test category (Music): create ->
   confirm the duplicate-detection query would catch a second confirm ->
   cleanup (delete). Self-cleaning: any leftover test coupon from an
   earlier run today is removed before assertions start, so repeated
   runs on the same day don't false-fail on stale state.

This is a test, not a report: any failed check exits non-zero.

Usage (from scripts/):
    cd scripts
    python verify_create_coupon.py
    cd ..
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from create_coupon import (
    build_coupon_code,
    build_payload,
    create_coupon,
    find_existing_coupon,
    get_env,
    get_verify_tls,
    resolve_category,
)
from dotenv import load_dotenv

CHECKS_PASSED = 0
CHECKS_FAILED: list[str] = []


def check(label: str, condition: bool) -> None:
    global CHECKS_PASSED
    if condition:
        CHECKS_PASSED += 1
    else:
        CHECKS_FAILED.append(label)
        print(f"FAIL: {label}")


def find_category_by_slug(
    base_url: str, key: str, secret: str, verify_ssl: bool, slug: str
) -> dict[str, Any]:
    resp = requests.get(
        f"{base_url}/wp-json/wc/v3/products/categories",
        params={"slug": slug},
        auth=(key, secret),
        verify=verify_ssl,
        timeout=30,
    )
    resp.raise_for_status()
    matches = resp.json()
    if not matches:
        raise RuntimeError(f"category slug {slug!r} not found in store")
    result: dict[str, Any] = matches[0]
    return result


def delete_coupon(
    base_url: str, key: str, secret: str, verify_ssl: bool, coupon_id: int
) -> None:
    resp = requests.delete(
        f"{base_url}/wp-json/wc/v3/coupons/{coupon_id}",
        params={"force": "true"},
        auth=(key, secret),
        verify=verify_ssl,
        timeout=30,
    )
    resp.raise_for_status()


def run_pure_logic_checks() -> None:
    code = build_coupon_code("electronics", 15, date(2026, 8, 27))
    check("code format: whole percent", code == "ELECTRONICS-15-20260827")

    code_frac = build_coupon_code("clothing", 12.5, date(2026, 1, 1))
    check("code format: fractional percent", code_frac == "CLOTHING-12_5-20260101")

    payload = build_payload("TESTCODE", 15, [67], date(2026, 9, 10))
    check("payload: code", payload["code"] == "TESTCODE")
    check("payload: discount_type", payload["discount_type"] == "percent")
    check("payload: amount formatted to 2 decimals", payload["amount"] == "15.00")
    check("payload: date_expires iso", payload["date_expires"] == "2026-09-10")
    check("payload: individual_use default True", payload["individual_use"] is True)
    check(
        "payload: exclude_sale_items default True",
        payload["exclude_sale_items"] is True,
    )
    check(
        "payload: usage_limit_per_user default 1",
        payload["usage_limit_per_user"] == 1,
    )
    check(
        "payload: product_categories passthrough", payload["product_categories"] == [67]
    )


def run_category_resolution_checks(
    base_url: str, key: str, secret: str, verify_ssl: bool
) -> None:
    electronics = find_category_by_slug(
        base_url, key, secret, verify_ssl, "electronics"
    )
    resolved = resolve_category(base_url, key, secret, verify_ssl, electronics["id"])
    check("electronics category resolves", resolved is not None)
    if resolved is not None:
        _, descendant_ids = resolved
        check(
            "electronics has no children (flat)", descendant_ids == [electronics["id"]]
        )

    clothing = find_category_by_slug(base_url, key, secret, verify_ssl, "clothing")
    resolved_clothing = resolve_category(
        base_url, key, secret, verify_ssl, clothing["id"]
    )
    check("clothing category resolves", resolved_clothing is not None)
    if resolved_clothing is not None:
        _, clothing_descendants = resolved_clothing
        check(
            "clothing subtree has 32 nodes (matches probe_category_children.py)",
            len(clothing_descendants) == 32,
        )
        leaf_ids = {
            find_category_by_slug(base_url, key, secret, verify_ssl, "tshirts")["id"],
            find_category_by_slug(base_url, key, secret, verify_ssl, "hoodies")["id"],
            find_category_by_slug(base_url, key, secret, verify_ssl, "accessories")[
                "id"
            ],
        }
        check(
            "clothing subtree contains known leaf categories",
            leaf_ids.issubset(set(clothing_descendants)),
        )


def run_live_round_trip_checks(
    base_url: str, key: str, secret: str, verify_ssl: bool, tz: ZoneInfo
) -> None:
    music = find_category_by_slug(base_url, key, secret, verify_ssl, "music")
    today = datetime.now(tz).date()
    test_discount = 1.5
    code = build_coupon_code(music["slug"], test_discount, today)

    leftover = find_existing_coupon(base_url, key, secret, verify_ssl, code)
    if leftover is not None:
        delete_coupon(base_url, key, secret, verify_ssl, leftover["id"])

    resolved = resolve_category(base_url, key, secret, verify_ssl, music["id"])
    check("music category resolves", resolved is not None)
    if resolved is None:
        return
    _, descendant_ids = resolved

    expires_on = today + timedelta(days=14)
    payload = build_payload(code, test_discount, descendant_ids, expires_on)

    before_create = find_existing_coupon(base_url, key, secret, verify_ssl, code)
    check("no duplicate before first create", before_create is None)

    created = create_coupon(base_url, key, secret, verify_ssl, payload)
    coupon_id = created.get("id")
    check("first create returns a coupon id", isinstance(coupon_id, int))
    check(
        "first create returns matching code",
        str(created.get("code", "")).upper() == code,
    )

    try:
        after_create = find_existing_coupon(base_url, key, secret, verify_ssl, code)
        check(
            "idempotency guard would find the existing coupon on a second confirm",
            after_create is not None and after_create["id"] == coupon_id,
        )
    finally:
        if isinstance(coupon_id, int):
            delete_coupon(base_url, key, secret, verify_ssl, coupon_id)

    after_delete = find_existing_coupon(base_url, key, secret, verify_ssl, code)
    check("coupon gone after cleanup delete", after_delete is None)


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    base_url = get_env("WOO_STORE_URL").rstrip("/")
    key = get_env("WOO_CONSUMER_KEY")
    secret = get_env("WOO_CONSUMER_SECRET")
    verify_ssl = get_verify_tls()
    tz = ZoneInfo(os.environ.get("BUSINESS_TIMEZONE", "Europe/Kyiv"))

    run_pure_logic_checks()
    run_category_resolution_checks(base_url, key, secret, verify_ssl)
    run_live_round_trip_checks(base_url, key, secret, verify_ssl, tz)

    print(f"\n{CHECKS_PASSED} checks passed, {len(CHECKS_FAILED)} failed")
    if CHECKS_FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
