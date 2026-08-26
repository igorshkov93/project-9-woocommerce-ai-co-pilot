"""Fetch the WooCommerce catalog into a JSON snapshot for vector indexing.

Fetching and embedding are deliberately separate stages. The snapshot can be
inspected, diffed and re-generated for free, while embeddings consume a limited
daily quota. Keeping them apart means a payload mistake costs a file rewrite,
not another round of API calls.

Only categories that actually contain products are kept: the store has many
empty placeholder categories, and offering them to the agent would let it
create coupons for categories nobody can buy from.

Dry-run by default. Pass --apply to write the snapshot to disk.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import urllib3
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "data" / "catalog.json"

# LocalWP is slow and serves a self-signed certificate. TLS verification is
# disabled on purpose for this local-only development stand; production
# deployments must leave it on.
VERIFY_TLS = False
REQUEST_TIMEOUT = 180
PER_PAGE = 100
MAX_PAGES = 50
MAX_DESCRIPTION_CHARS = 400

TAG_PATTERN = re.compile(r"<[^>]+>")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def resolve_wp_json_base(raw_base: str) -> str:
    """Derive the /wp-json root from any WordPress REST URL.

    COPILOT_API_BASE points at the mu-plugin namespace
    (https://site/wp-json/copilot/v1) because that is what the abandoned-cart
    scripts need. Everything from /wp-json onwards is dropped and rebuilt, so
    both a bare site URL and a namespaced one resolve to the same root.
    """
    base = raw_base.strip().rstrip("/")
    marker = "/wp-json"
    index = base.find(marker)
    if index != -1:
        base = base[:index]
    return f"{base}{marker}"


def clean_text(value: str | None) -> str:
    """Strip HTML tags, decode entities and collapse whitespace."""
    if not value:
        return ""
    without_tags = TAG_PATTERN.sub(" ", value)
    unescaped = html.unescape(without_tags)
    return " ".join(unescaped.split())


def fetch_paginated(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Collect every page of a WooCommerce list endpoint."""
    collected: list[dict[str, Any]] = []
    page = 1
    while page <= MAX_PAGES:
        page_params = dict(params, per_page=PER_PAGE, page=page)
        response = session.get(
            url,
            params=page_params,
            timeout=REQUEST_TIMEOUT,
            verify=VERIFY_TLS,
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise RuntimeError(
                f"Expected a list from {url}, got {type(batch).__name__}"
            )
        collected.extend(batch)
        if len(batch) < PER_PAGE:
            return collected
        page += 1
    raise RuntimeError(f"Pagination guard tripped after {MAX_PAGES} pages at {url}")


def normalise_product(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce a WooCommerce product to the fields the copilot actually needs."""
    categories = [
        {"id": int(item["id"]), "name": html.unescape(str(item.get("name", "")))}
        for item in raw.get("categories", [])
    ]
    description = clean_text(raw.get("short_description")) or clean_text(
        raw.get("description")
    )
    return {
        "entity_type": "product",
        "entity_id": int(raw["id"]),
        "name": html.unescape(str(raw.get("name", ""))),
        "slug": str(raw.get("slug", "")),
        "sku": str(raw.get("sku", "")) or None,
        "product_type": str(raw.get("type", "")),
        "status": str(raw.get("status", "")),
        "price": str(raw.get("price", "")) or None,
        "stock_status": str(raw.get("stock_status", "")),
        "category_ids": [item["id"] for item in categories],
        "category_names": [item["name"] for item in categories],
        "description": description[:MAX_DESCRIPTION_CHARS],
    }


def normalise_category(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce a WooCommerce category to the fields the copilot actually needs."""
    return {
        "entity_type": "category",
        "entity_id": int(raw["id"]),
        "name": html.unescape(str(raw.get("name", ""))),
        "slug": str(raw.get("slug", "")),
        "parent_id": int(raw.get("parent", 0)),
        "product_count": int(raw.get("count", 0)),
        "description": clean_text(raw.get("description"))[:MAX_DESCRIPTION_CHARS],
    }


def report_duplicates(label: str, names: list[str]) -> None:
    """Print names shared by more than one entity - the case exact matching fails."""
    duplicates = {name: count for name, count in Counter(names).items() if count > 1}
    if not duplicates:
        print(f"  {label}: no duplicate names")
        return
    print(f"  {label}: {len(duplicates)} duplicated name(s)")
    for name, count in sorted(duplicates.items()):
        print(f"    - {name!r} x{count}")


def build_snapshot(base_url: str, auth: HTTPBasicAuth) -> dict[str, Any]:
    """Fetch products and non-empty categories from the WooCommerce REST API."""
    session = requests.Session()
    session.auth = auth

    products_url = f"{base_url}/wc/v3/products"
    categories_url = f"{base_url}/wc/v3/products/categories"

    print("Fetching products ...")
    raw_products = fetch_paginated(session, products_url, {"status": "publish"})
    print(f"  received {len(raw_products)} product(s)")

    print("Fetching categories ...")
    raw_categories = fetch_paginated(session, categories_url, {"hide_empty": "false"})
    print(f"  received {len(raw_categories)} category(ies)")

    products = [normalise_product(item) for item in raw_products]
    all_categories = [normalise_category(item) for item in raw_categories]
    categories = [item for item in all_categories if item["product_count"] > 0]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": base_url,
        "stats": {
            "products": len(products),
            "categories_total": len(all_categories),
            "categories_indexed": len(categories),
        },
        "products": products,
        "categories": categories,
        "_all_category_names": [item["name"] for item in all_categories],
    }


def print_summary(snapshot: dict[str, Any]) -> None:
    """Show what was fetched and why vector search is needed."""
    stats = snapshot["stats"]
    print("\nSummary")
    print(f"  products: {stats['products']}")
    print(
        f"  categories: {stats['categories_indexed']} non-empty "
        f"of {stats['categories_total']} total"
    )

    type_counts = Counter(item["product_type"] for item in snapshot["products"])
    print(
        "  product types: "
        + ", ".join(f"{key}={value}" for key, value in sorted(type_counts.items()))
    )

    print("\nName collisions (why exact string matching is not enough)")
    report_duplicates("all categories", snapshot["_all_category_names"])
    report_duplicates(
        "indexed categories", [item["name"] for item in snapshot["categories"]]
    )
    report_duplicates("products", [item["name"] for item in snapshot["products"]])

    print("\nCategories to index")
    for item in sorted(snapshot["categories"], key=lambda entry: str(entry["name"])):
        print(
            f"  id={item['entity_id']:>4}  count={item['product_count']:>3}  {item['name']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the snapshot to disk (otherwise the run is read-only)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"snapshot path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")

    raw_base = os.getenv("COPILOT_API_BASE", "")
    user = os.getenv("WP_APP_USER", "")
    password = os.getenv("WP_APP_PASSWORD", "")

    missing = [
        name
        for name, value in (
            ("COPILOT_API_BASE", raw_base),
            ("WP_APP_USER", user),
            ("WP_APP_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        print(
            f"ERROR: missing environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    base_url = resolve_wp_json_base(raw_base)
    print(f"WooCommerce REST base: {base_url}")

    try:
        snapshot = build_snapshot(base_url, HTTPBasicAuth(user, password))
    except requests.HTTPError as error:
        print(
            f"ERROR: HTTP {error.response.status_code} from {error.response.url}",
            file=sys.stderr,
        )
        return 1
    except (requests.RequestException, TimeoutError, RuntimeError) as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print_summary(snapshot)

    if not snapshot["products"]:
        print("\nERROR: no products fetched, refusing to continue", file=sys.stderr)
        return 1

    payload = {key: value for key, value in snapshot.items() if not key.startswith("_")}

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the snapshot.")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
