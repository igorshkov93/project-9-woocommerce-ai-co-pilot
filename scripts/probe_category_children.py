"""
probe_category_children.py

Step 13, sub-step 13.1 — probe the WooCommerce categories tree to confirm
the shape of GET /products/categories, check whether "Electronics" (id 67)
has child categories, and locate a category that DOES have children (e.g.
Clothing, per project notes: product_count counts descendants) so the
resolver logic in create_coupon.py gets exercised against a real
multi-level case, not just a flat one.

Read-only. No --apply flag: this script never writes anything.

Usage (from scripts/):
    cd scripts
    python probe_category_children.py --category-id 67
    python probe_category_children.py --list-nonempty
    cd ..
"""

from __future__ import annotations

import argparse
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
    # NOTE: the .env key is WOO_VERIFY_TLS (see known debt in project notes:
    # woo_client.py reads WOO_VERIFY_SSL, .env.example has WOO_VERIFY_TLS).
    # This probe uses the .env key as it actually exists today.
    raw = os.environ.get("WOO_VERIFY_TLS", "false")
    return raw.strip().lower() not in ("false", "0", "no")


def fetch_all_categories(
    base_url: str, key: str, secret: str, verify_ssl: bool
) -> list[dict[str, Any]]:
    """Fetch every product category in one paginated sweep."""
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
            print("WARNING: hit page safety cap (50)", file=sys.stderr)
            break
    return categories


def build_children_index(
    categories: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    index: dict[int, list[dict[str, Any]]] = {}
    for cat in categories:
        parent_id = cat["parent"]
        index.setdefault(parent_id, []).append(cat)
    return index


def print_subtree(
    category_id: int,
    by_id: dict[int, dict[str, Any]],
    children_of: dict[int, list[dict[str, Any]]],
    depth: int = 0,
) -> list[int]:
    cat = by_id[category_id]
    prefix = "  " * depth + ("- " if depth else "")
    print(
        f"{prefix}id={cat['id']} slug={cat['slug']} name={cat['name']!r} "
        f"count={cat['count']} parent={cat['parent']}"
    )
    collected = [category_id]
    for child in children_of.get(category_id, []):
        collected.extend(print_subtree(child["id"], by_id, children_of, depth + 1))
    return collected


def list_nonempty(categories: list[dict[str, Any]]) -> None:
    nonempty = sorted(
        (c for c in categories if c["count"] > 0), key=lambda c: -c["count"]
    )
    print(f"Non-empty categories: {len(nonempty)} of {len(categories)} total")
    for c in nonempty:
        marker = " <-- has parent (nested)" if c["parent"] != 0 else ""
        print(
            f"id={c['id']:>4} parent={c['parent']:>4} count={c['count']:>4} "
            f"slug={c['slug']!r} name={c['name']!r}{marker}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--category-id",
        type=int,
        default=None,
        help="Root category id to inspect as a subtree",
    )
    parser.add_argument(
        "--list-nonempty",
        action="store_true",
        help="List all categories with count > 0, flagging nested ones",
    )
    args = parser.parse_args()

    if args.category_id is None and not args.list_nonempty:
        args.category_id = 67

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

    base_url = get_env("WOO_STORE_URL").rstrip("/")
    key = get_env("WOO_CONSUMER_KEY")
    secret = get_env("WOO_CONSUMER_SECRET")
    verify_ssl = get_verify_tls()

    categories = fetch_all_categories(base_url, key, secret, verify_ssl)
    by_id = {c["id"]: c for c in categories}

    if args.list_nonempty:
        list_nonempty(categories)
        return

    if args.category_id not in by_id:
        print(
            f"ERROR: category id {args.category_id} not found among "
            f"{len(categories)} categories",
            file=sys.stderr,
        )
        sys.exit(1)

    children_of = build_children_index(categories)

    print(f"Total categories in store: {len(categories)}")
    print(f"Subtree rooted at id={args.category_id}:")
    ids = print_subtree(args.category_id, by_id, children_of)
    print()
    print(f"Descendant count (including root): {len(ids)}")
    print(f"Descendant ids: {ids}")

    root = by_id[args.category_id]
    descendants_count_sum = sum(by_id[cid]["count"] for cid in ids[1:] if cid in by_id)
    print(f"Root count field: {root['count']} (WooCommerce counts descendants here)")
    print(f"Sum of descendant counts: {descendants_count_sum}")


if __name__ == "__main__":
    main()
