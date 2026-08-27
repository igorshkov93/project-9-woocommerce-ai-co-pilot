"""
verify_draft_abandoned_cart_email.py

Step 14, sub-step 14.4 - verification suite for
draft_abandoned_cart_email.py.

Two layers:
1. Pure logic (deterministic, fixture-based): select_target_cart's
   sort/tie-break/recovered-filter behavior, format_cart_for_prompt's
   exact output, strip_code_fence's markdown-fence handling.
2. Live (one real Haiku call, costs a fraction of a cent): fetches the
   real abandoned-carts endpoint, confirms the selected cart is genuinely
   the highest-value non-recovered one in the live data, and checks the
   generated email is grounded in real cart facts (product name present,
   no unfilled placeholders, no fabricated discount pattern).

This is a test, not a report: any failed check exits non-zero.

Usage (from scripts/):
    cd scripts
    python verify_draft_abandoned_cart_email.py
    cd ..
"""

from __future__ import annotations

import os
import re
import sys

from dotenv import load_dotenv
from draft_abandoned_cart_email import (
    call_haiku,
    fetch_abandoned_carts,
    format_cart_for_prompt,
    get_env,
    get_verify_tls,
    select_target_cart,
    site_root_from_copilot_base,
    strip_code_fence,
)

CHECKS_PASSED = 0
CHECKS_FAILED: list[str] = []


def check(label: str, condition: bool) -> None:
    global CHECKS_PASSED
    if condition:
        CHECKS_PASSED += 1
    else:
        CHECKS_FAILED.append(label)
        print(f"FAIL: {label}")


def run_select_target_cart_checks() -> None:
    carts = [
        {"id": 1, "cart_total": 100, "minutes_since_update": 50, "recovered": False},
        {"id": 2, "cart_total": 150, "minutes_since_update": 30, "recovered": False},
        {"id": 3, "cart_total": 150, "minutes_since_update": 10, "recovered": False},
        {"id": 4, "cart_total": 500, "minutes_since_update": 5, "recovered": True},
    ]

    picked = select_target_cart(carts, None)
    check(
        "tie-break: lower minutes_since_update wins on equal cart_total",
        picked is not None and picked["id"] == 3,
    )

    check(
        "recovered carts excluded even with highest cart_total",
        picked is not None and picked["id"] != 4,
    )

    by_id = select_target_cart(carts, cart_id=1)
    check(
        "explicit cart_id override returns that cart",
        by_id is not None and by_id["id"] == 1,
    )

    recovered_by_id = select_target_cart(carts, cart_id=4)
    check("explicit cart_id for a recovered cart returns None", recovered_by_id is None)

    check("empty cart list returns None", select_target_cart([], None) is None)

    all_recovered = [
        {"id": 9, "cart_total": 999, "minutes_since_update": 1, "recovered": True}
    ]
    check(
        "all-recovered list returns None",
        select_target_cart(all_recovered, None) is None,
    )


def run_format_cart_for_prompt_checks() -> None:
    cart = {
        "first_name": "TestUser",
        "minutes_since_update": 42,
        "currency": "USD",
        "items": [{"name": "Widget", "quantity": 2, "price": 10.5, "line_total": 21.0}],
        "cart_total": 21.0,
    }
    expected = (
        "Customer: TestUser\n"
        "Cart abandoned: 42 minutes ago\n"
        "Currency: USD\n"
        "Items:\n"
        "- Widget x2 @ USD 10.50 = USD 21.00\n"
        "Cart total: USD 21.00"
    )
    check(
        "format_cart_for_prompt produces expected exact text",
        format_cart_for_prompt(cart) == expected,
    )


def run_strip_code_fence_checks() -> None:
    check(
        "strips ```json fenced block",
        strip_code_fence('```json\n{"a":1}\n```') == '{"a":1}',
    )
    check(
        "strips plain ``` fenced block",
        strip_code_fence('```\n{"a":1}\n```') == '{"a":1}',
    )
    check(
        "leaves unfenced text unchanged",
        strip_code_fence('{"a":1}') == '{"a":1}',
    )


def run_live_checks() -> None:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    copilot_api_base = get_env("COPILOT_API_BASE")
    wp_user = get_env("WP_APP_USER")
    wp_password = get_env("WP_APP_PASSWORD")
    anthropic_key = get_env("ANTHROPIC_API_KEY")
    verify_ssl = get_verify_tls()
    site_root = site_root_from_copilot_base(copilot_api_base)

    response = fetch_abandoned_carts(site_root, wp_user, wp_password, verify_ssl)
    carts = response["carts"]
    check("live response has at least one cart", len(carts) > 0)

    target = select_target_cart(carts, None)
    check("a target cart was selected from live data", target is not None)
    if target is None:
        return

    non_recovered = [c for c in carts if not c.get("recovered", False)]
    max_total = max(c["cart_total"] for c in non_recovered)
    check(
        "selected cart's total is the max among non-recovered live carts",
        target["cart_total"] == max_total,
    )

    user_message = format_cart_for_prompt(target)
    draft = call_haiku(anthropic_key, user_message)

    check("draft has non-empty subject", bool(draft.get("subject", "").strip()))
    check("draft has non-empty body", bool(draft.get("body", "").strip()))
    check("subject length is reasonable", len(draft.get("subject", "")) < 200)
    check("body length is reasonable", 30 < len(draft.get("body", "")) < 3000)

    body = draft.get("body", "")
    item_names = [item["name"] for item in target["items"]]
    check(
        "body mentions at least one real product name from the target cart",
        any(name in body for name in item_names),
    )
    check(
        "no unfilled template placeholders in body",
        not re.search(r"\{\{.*?\}\}|\{[a-zA-Z_]+\}", body),
    )
    check(
        "no fabricated discount percentage pattern in body",
        not re.search(r"\d{1,2}\s*%\s*(off|discount|скидк)", body, re.IGNORECASE),
    )


def main() -> None:
    run_select_target_cart_checks()
    run_format_cart_for_prompt_checks()
    run_strip_code_fence_checks()
    run_live_checks()

    print(f"\n{CHECKS_PASSED} checks passed, {len(CHECKS_FAILED)} failed")
    if CHECKS_FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
