"""
draft_abandoned_cart_email.py

Step 14, sub-step 14.3 - reference implementation for the
draft_abandoned_cart_email tool ("Напиши email-кампанию для брошенных
корзин").

Single-phase, read+generate tool (no store mutation, no propose/confirm):
fetches abandoned carts from the copilot-abandoned-carts mu-plugin route,
picks the highest-value non-recovered cart (ties broken by most recently
abandoned), and asks Claude Haiku to draft a recovery email grounded only
in the real cart data.

Contract: ok | no_data | invalid_input | upstream_error

Usage (from scripts/):
    cd scripts
    python draft_abandoned_cart_email.py
    python draft_abandoned_cart_email.py --cart-id 8
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

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_API_VERSION = "2023-06-01"

SYSTEM_PROMPT = """You are a customer-retention email copywriter for an online store selling electronics and lifestyle products. Write a short, warm, low-pressure cart-recovery email for the customer described in the user message.

Rules:
- Use ONLY the facts given: product names, quantities, prices, cart total, currency, first name.
- Do NOT invent stock levels, deadlines, discounts, coupon codes, or any claim not present in the provided data.
- Do not offer or mention any discount, percentage off, or promo code - none has been issued for this email.
- Keep the tone friendly and low-pressure, in English.
- Respond with ONLY a single JSON object, no markdown code fences, no extra commentary, in exactly this shape:
{"subject": "<email subject line>", "body": "<plain text email body>"}
"""


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


def emit(status: str, **extra: Any) -> None:
    print(json.dumps({"status": status, **extra}, indent=2, ensure_ascii=False))


def fail(status: str, **extra: Any) -> None:
    emit(status, **extra)
    sys.exit(1)


def fetch_abandoned_carts(
    site_root: str, wp_user: str, wp_password: str, verify_ssl: bool
) -> dict[str, Any]:
    resp = requests.get(
        f"{site_root}/wp-json/copilot/v1/abandoned-carts",
        auth=(wp_user, wp_password),
        verify=verify_ssl,
        timeout=30,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def select_target_cart(
    carts: list[dict[str, Any]], cart_id: int | None
) -> dict[str, Any] | None:
    candidates = [c for c in carts if not c.get("recovered", False)]
    if cart_id is not None:
        for cart in candidates:
            if cart["id"] == cart_id:
                return cart
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c["cart_total"], c["minutes_since_update"]))
    return candidates[0]


def format_cart_for_prompt(cart: dict[str, Any]) -> str:
    currency = cart["currency"]
    lines = [
        f"Customer: {cart['first_name']}",
        f"Cart abandoned: {cart['minutes_since_update']} minutes ago",
        f"Currency: {currency}",
        "Items:",
    ]
    for item in cart["items"]:
        lines.append(
            f"- {item['name']} x{item['quantity']} @ {currency} {item['price']:.2f} "
            f"= {currency} {item['line_total']:.2f}"
        )
    lines.append(f"Cart total: {currency} {cart['cart_total']:.2f}")
    return "\n".join(lines)


def strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def call_haiku(api_key: str, user_message: str) -> dict[str, str]:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 500,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()

    text_blocks = [
        block.get("text", "")
        for block in payload.get("content", [])
        if block.get("type") == "text"
    ]
    text = strip_code_fence("".join(text_blocks))

    if not text:
        print("DEBUG raw payload:", file=sys.stderr)
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
        raise RuntimeError(
            f"empty text in response; stop_reason={payload.get('stop_reason')!r}"
        )

    parsed: dict[str, str] = json.loads(text)
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cart-id", type=int, default=None)
    args = parser.parse_args()

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    copilot_api_base = get_env("COPILOT_API_BASE")
    wp_user = get_env("WP_APP_USER")
    wp_password = get_env("WP_APP_PASSWORD")
    anthropic_key = get_env("ANTHROPIC_API_KEY")
    verify_ssl = get_verify_tls()
    site_root = site_root_from_copilot_base(copilot_api_base)

    try:
        response = fetch_abandoned_carts(site_root, wp_user, wp_password, verify_ssl)
    except requests.RequestException as exc:
        fail("upstream_error", message=str(exc))
        return

    cart = select_target_cart(response["carts"], args.cart_id)
    if cart is None:
        fail("no_data", message="no eligible abandoned cart found")
        return

    user_message = format_cart_for_prompt(cart)

    try:
        draft = call_haiku(anthropic_key, user_message)
    except (
        requests.RequestException,
        KeyError,
        IndexError,
        json.JSONDecodeError,
        RuntimeError,
    ) as exc:
        fail("upstream_error", message=f"LLM call/parse failed: {exc}")
        return

    if "subject" not in draft or "body" not in draft:
        fail("upstream_error", message="LLM response missing subject/body")
        return

    emit(
        "ok",
        cart={
            "id": cart["id"],
            "email": cart["email"],
            "first_name": cart["first_name"],
            "cart_total": cart["cart_total"],
            "currency": cart["currency"],
        },
        subject=draft["subject"],
        body=draft["body"],
    )


if __name__ == "__main__":
    main()