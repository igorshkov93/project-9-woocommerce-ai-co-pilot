"""Delete legacy orders left over from earlier experiments.

Those orders were created in UAH while the store now runs in USD, and they
reference product ids that no longer exist. WooCommerce freezes the currency at
creation time and never converts it, so mixing them with fresh data would make
every revenue figure meaningless.

Selection is by currency, never by date: the legacy date range overlaps the
window the order generator is about to fill, while the currency is unambiguous.

Read-only by default; pass --apply to delete. Deletion is permanent (force=true)
because trashed orders still surface in status=any queries.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from woo_client import PROJECT_ROOT, WooClient, WooError

# Refuse to run if the selection looks too large: that would mean the script is
# being run after the generator, against real seeded data.
MAX_DELETIONS = 15

BACKUP_DIR = PROJECT_ROOT / "backups"


def write_backup(orders: list[dict]) -> Path:
    """Persist the full payload of the orders about to be deleted."""
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = BACKUP_DIR / f"legacy_orders_{stamp}.json"
    path.write_text(json.dumps(orders, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge non-USD legacy orders.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without it the script only reports.",
    )
    args = parser.parse_args()

    print(f"mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print()

    try:
        client = WooClient()
        store_currency = client.get("system_status")["settings"]["currency"]
        print(f"store currency: {store_currency}")

        orders = client.get_all("orders", {"status": "any"})
        print(f"orders found:   {len(orders)}")
        print()

        doomed: list[dict] = []
        for order in sorted(orders, key=lambda o: o["id"]):
            currency = order.get("currency")
            legacy = currency != store_currency
            marker = "LEGACY" if legacy else "keep  "
            print(
                f"{marker} id={order['id']:<6} "
                f"currency={currency:<5} "
                f"status={order.get('status'):<12} "
                f"total={order.get('total'):<10} "
                f"created={order.get('date_created_gmt')}"
            )
            if legacy:
                doomed.append(order)

        print()
        print(f"selected for deletion: {len(doomed)}")

        if not doomed:
            print("Nothing to do.")
            return 0

        if len(doomed) > MAX_DELETIONS:
            print(
                f"ABORT: selection exceeds the safety limit of {MAX_DELETIONS}. "
                "Refusing to delete. Inspect the store manually."
            )
            return 1

        if not args.apply:
            print()
            print("Nothing was deleted. Re-run with --apply to commit.")
            return 0

        backup_path = write_backup(doomed)
        print(f"backup written: {backup_path}")
        print()

        for order in doomed:
            client.delete(f"orders/{order['id']}", {"force": "true"})
            print(f"DELETED id={order['id']}")

        print()
        print(f"orders remaining: {client.count('orders', {'status': 'any'})}")

    except WooError as error:
        print(f"FAIL: {error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
