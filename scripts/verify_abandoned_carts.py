"""Verification test for the abandoned carts endpoint.

Run from the scripts directory:
    cd scripts; python verify_abandoned_carts.py; cd ..

This is a test, not a report. It exits with a non-zero status when any
assertion fails, so a regression is impossible to miss. Every check here
corresponds to a failure mode that would silently produce wrong automation
behaviour rather than an obvious error.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from copilot_client import CopilotClient, CopilotClientError

# Seeded expectations from seed_abandoned_carts.php. Kept here rather than
# derived from the API, so that a broken seed is detected instead of excused.
SEED_PREFIX_COUNT = 7
SEED_WITHOUT_EMAIL = 1
SEED_RECOVERED = 1

failures: list[str] = []
checks_run = 0


def check(condition: bool, description: str, detail: str = "") -> None:
    """Records the outcome of a single assertion."""
    global checks_run
    checks_run += 1

    if condition:
        print(f"  PASS  {description}")
        return

    message = description if not detail else f"{description} -- {detail}"
    print(f"  FAIL  {message}")
    failures.append(message)


def parse_iso(value: str) -> datetime:
    """Parses the ISO 8601 timestamps the endpoint returns.

    The Z suffix is not understood by fromisoformat before Python 3.11, and
    normalising it here keeps the script portable across the versions used in
    this project.
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    return datetime.fromisoformat(value)


def main() -> int:
    try:
        client = CopilotClient.from_env()
    except CopilotClientError as error:
        print(f"FATAL: {error}")
        return 1

    print("Route availability and authorization")

    try:
        status, body = client.get("abandoned-carts", authenticated=False)
    except CopilotClientError as error:
        print(f"FATAL: {error}")
        return 1

    check(
        status != 404,
        "route is registered",
        "got 404: the plugin is probably not deployed to mu-plugins",
    )
    check(
        status == 401,
        "anonymous access is rejected",
        f"expected 401, got {status}",
    )

    if status == 200:
        # Worth spelling out: this is a data leak, not a failing test.
        check(
            False, "anonymous request must not return data", "customer emails exposed"
        )

    try:
        status, body = client.get_abandoned_carts(
            threshold_minutes=5,
            include_recovered=True,
            require_email=False,
            limit=100,
        )
    except CopilotClientError as error:
        print(f"FATAL: {error}")
        return 1

    check(status == 200, "authenticated request succeeds", f"got {status}: {body}")

    if status != 200:
        print("\nCannot continue without a successful response.")
        return 1

    print("\nResponse envelope")

    for field in (
        "generated_at_gmt",
        "generated_at_local",
        "timezone",
        "threshold_minutes",
        "store_currency",
        "count",
        "carts",
    ):
        check(field in body, f"envelope contains {field}")

    check(
        body.get("timezone") not in ("", "+00:00", "UTC"),
        "store timezone is configured, not UTC",
        f"timezone is {body.get('timezone')!r}: local timestamps would mislead the model",
    )

    carts: list[dict[str, Any]] = body.get("carts", [])

    check(
        body.get("count") == len(carts),
        "count matches the number of returned carts",
        f"count={body.get('count')} len={len(carts)}",
    )

    print("\nSeeded data")

    no_email = [cart for cart in carts if not cart.get("email")]
    recovered = [cart for cart in carts if cart.get("recovered")]

    check(
        len(carts) >= SEED_PREFIX_COUNT,
        f"at least {SEED_PREFIX_COUNT} carts are present",
        f"found {len(carts)}: re-run seed_abandoned_carts.php with apply",
    )
    check(
        len(no_email) >= SEED_WITHOUT_EMAIL,
        "a cart without an email address exists",
        "the require_email filter would go untested",
    )
    check(
        len(recovered) >= SEED_RECOVERED,
        "a recovered cart exists",
        "the recovery filter would go untested",
    )

    print("\nPer-cart integrity")

    for cart in carts:
        label = f"cart {cart.get('id')}"
        items = cart.get("items", [])

        if not isinstance(items, list) or not items:
            check(False, f"{label}: items is a non-empty list")
            continue

        computed = round(sum(float(item["line_total"]) for item in items), 2)
        stored = round(float(cart.get("cart_total", 0)), 2)

        check(
            abs(computed - stored) < 0.01,
            f"{label}: cart_total matches the sum of line totals",
            f"stored={stored} computed={computed}",
        )

        quantities = sum(int(item["quantity"]) for item in items)

        check(
            quantities == int(cart.get("items_count", -1)),
            f"{label}: items_count matches the sum of quantities",
            f"stored={cart.get('items_count')} computed={quantities}",
        )

        check(
            all(item.get("sku") for item in items),
            f"{label}: every item carries a SKU",
        )

    print("\nTimezone handling")

    if carts:
        sample = carts[0]
        gmt = parse_iso(sample["updated_gmt"])
        local = parse_iso(sample["updated_local"])

        check(
            gmt == local,
            "updated_local is the same instant as updated_gmt",
            f"{sample['updated_gmt']} vs {sample['updated_local']}",
        )
        check(
            local.utcoffset() != timedelta(0),
            "updated_local carries a non-zero offset",
            "the store is running in UTC, which defeats the whole timezone story",
        )

    print("\nFiltering")

    status, strict = client.get_abandoned_carts(
        threshold_minutes=5,
        include_recovered=False,
        require_email=True,
        limit=100,
    )

    strict_carts: list[dict[str, Any]] = strict.get("carts", [])

    check(
        all(not cart.get("recovered") for cart in strict_carts),
        "recovered carts are excluded by default",
        "the automation would email customers who already paid",
    )
    check(
        all(cart.get("email") for cart in strict_carts),
        "carts without an email are excluded when require_email is true",
    )
    check(
        len(strict_carts) < len(carts),
        "the strict query returns fewer carts than the permissive one",
        f"strict={len(strict_carts)} permissive={len(carts)}",
    )

    now = datetime.now(UTC)
    threshold_minutes = 240

    status, windowed = client.get_abandoned_carts(
        threshold_minutes=threshold_minutes,
        include_recovered=True,
        require_email=False,
        limit=100,
    )

    windowed_carts: list[dict[str, Any]] = windowed.get("carts", [])

    check(
        all(
            (now - parse_iso(cart["updated_gmt"])).total_seconds()
            >= threshold_minutes * 60
            for cart in windowed_carts
        ),
        f"every cart returned at threshold={threshold_minutes} is genuinely that idle",
    )
    check(
        len(windowed_carts) < len(carts),
        "a larger threshold narrows the result set",
        f"threshold={threshold_minutes} returned {len(windowed_carts)} of {len(carts)}",
    )

    print(f"\n{checks_run} checks run, {len(failures)} failed.")

    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
