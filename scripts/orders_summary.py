"""Reference implementation of the get_orders_summary tool.

Read-only: computes order metrics for a business-timezone window and prints
them in the tool contract shape. The n8n sub-workflow must match this output
to the cent; this file is the source of truth for the comparison.

Refunds are attributed to the date of their PARENT ORDER, not the refund date.
Two reasons: the orders endpoint carries refund totals but no refund dates
(per-order attribution would cost one extra request per order), and refund
dates in the seeded demo data all collapse onto the seeding day.

Usage:
    python orders_summary.py --period today
    python orders_summary.py --period last_7_days --json
    python orders_summary.py --period custom --date-from 2026-08-15 \
        --date-to 2026-08-21
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import urllib3
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

UTC = ZoneInfo("UTC")
TIMEOUT = 30
MAX_PAGES = 50
PER_PAGE = 100
MAX_WINDOW_DAYS = 92

ALL_STATUSES = (
    "pending",
    "processing",
    "on-hold",
    "completed",
    "cancelled",
    "refunded",
    "failed",
)
REVENUE_STATUSES = frozenset({"pending", "processing", "on-hold", "completed"})

PERIODS = ("today", "yesterday", "last_7_days", "last_30_days", "custom")

STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"
STATUS_INVALID = "invalid_input"
STATUS_UPSTREAM = "upstream_error"


class ToolError(Exception):
    """Raised when the tool must return a non-ok status."""

    def __init__(self, status: str, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def env(name: str, default: str | None = None) -> str:
    """Return an environment variable, or raise a configuration error."""
    value = os.getenv(name, "").strip()
    if not value:
        if default is not None:
            return default
        raise ToolError(STATUS_UPSTREAM, "missing_config", f"{name} is not set in .env")
    return value


def tls_verify() -> bool:
    """Whether to verify TLS. LocalWP ships a self-signed certificate."""
    return env("WOO_VERIFY_TLS", "false").lower() in {"1", "true", "yes"}


def money(value: float) -> float:
    """Round a monetary amount to two decimals."""
    return round(value + 0.0, 2)


def parse_gmt(value: str) -> datetime:
    """Parse a WooCommerce GMT timestamp into an aware UTC datetime."""
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def utc_str(dt: datetime) -> str:
    """Format an aware datetime as the naive UTC string the REST API wants."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def parse_day(value: str, field: str) -> date:
    """Parse a YYYY-MM-DD string or raise invalid_input."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(
            STATUS_INVALID, "bad_date", f"{field} must be YYYY-MM-DD, got {value!r}"
        ) from None


def resolve_window(
    period: str, date_from: str | None, date_to: str | None, tz: ZoneInfo
) -> tuple[date, date, str]:
    """Resolve a period name into an inclusive [first_day, last_day] range."""
    if period not in PERIODS:
        raise ToolError(
            STATUS_INVALID,
            "bad_period",
            f"period must be one of {', '.join(PERIODS)}, got {period!r}",
        )

    today = datetime.now(tz).date()

    if period == "today":
        return today, today, "Today"
    if period == "yesterday":
        day = today - timedelta(days=1)
        return day, day, "Yesterday"
    if period == "last_7_days":
        return today - timedelta(days=6), today, "Last 7 days"
    if period == "last_30_days":
        return today - timedelta(days=29), today, "Last 30 days"

    if not date_from or not date_to:
        raise ToolError(
            STATUS_INVALID,
            "missing_dates",
            "period=custom requires both date_from and date_to",
        )
    first = parse_day(date_from, "date_from")
    last = parse_day(date_to, "date_to")
    if last < first:
        raise ToolError(
            STATUS_INVALID, "inverted_range", "date_to must not precede date_from"
        )
    span = (last - first).days + 1
    if span > MAX_WINDOW_DAYS:
        raise ToolError(
            STATUS_INVALID,
            "window_too_large",
            f"window spans {span} days, maximum is {MAX_WINDOW_DAYS}",
        )
    return first, last, f"{first.isoformat()} to {last.isoformat()}"


def to_bounds(first: date, last: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Convert an inclusive day range into aware [start, end) datetimes."""
    start = datetime.combine(first, time.min, tzinfo=tz)
    end = datetime.combine(last, time.min, tzinfo=tz) + timedelta(days=1)
    return start, end


def fetch_orders(
    session: requests.Session, url: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Fetch every order in [start, end), compensating exclusive bounds."""
    orders: list[dict[str, Any]] = []
    verify = tls_verify()
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    for page in range(1, MAX_PAGES + 1):
        params: dict[str, Any] = {
            "per_page": PER_PAGE,
            "page": page,
            "status": ",".join(ALL_STATUSES),
            "dates_are_gmt": "true",
            "after": utc_str(start - timedelta(seconds=1)),
            "before": utc_str(end),
            "orderby": "date",
            "order": "asc",
        }
        try:
            response = session.get(url, params=params, timeout=TIMEOUT, verify=verify)
        except requests.RequestException as exc:
            raise ToolError(
                STATUS_UPSTREAM, "request_failed", f"WooCommerce request failed: {exc}"
            ) from exc
        if response.status_code != 200:
            raise ToolError(
                STATUS_UPSTREAM,
                "http_error",
                f"WooCommerce returned HTTP {response.status_code}",
            )
        orders.extend(response.json())
        if page >= int(response.headers.get("X-WP-TotalPages", "1")):
            break
    return orders


def compute(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a list of orders into the metric block."""
    by_status: dict[str, dict[str, Any]] = {}
    gross = 0.0
    refunds = 0.0
    revenue_orders = 0
    currency = ""

    for order in orders:
        status = str(order["status"])
        total = float(order["total"])
        currency = currency or str(order.get("currency", ""))

        bucket = by_status.setdefault(status, {"count": 0, "total": 0.0})
        bucket["count"] += 1
        bucket["total"] += total

        if status in REVENUE_STATUSES:
            gross += total
            revenue_orders += 1
            for refund in order.get("refunds") or []:
                refunds += abs(float(refund["total"]))

    for bucket in by_status.values():
        bucket["total"] = money(bucket["total"])

    return {
        "orders_count": len(orders),
        "revenue_orders_count": revenue_orders,
        "revenue_gross": money(gross),
        "refunds_total": money(refunds),
        "revenue_net": money(gross - refunds),
        "aov": money(gross / revenue_orders) if revenue_orders else None,
        "currency": currency or None,
        "by_status": dict(sorted(by_status.items())),
    }


def delta(current: float | None, previous: float | None) -> dict[str, Any]:
    """Absolute and percentage change, guarding division by zero."""
    if current is None or previous is None:
        return {"absolute": None, "percent": None}
    diff = money(current - previous)
    if previous == 0:
        return {"absolute": diff, "percent": None}
    return {"absolute": diff, "percent": round(diff / previous * 100, 1)}


def build(
    session: requests.Session,
    url: str,
    tz: ZoneInfo,
    period: str,
    date_from: str | None,
    date_to: str | None,
    compare: bool,
) -> dict[str, Any]:
    """Produce the full contract payload."""
    first, last, label = resolve_window(period, date_from, date_to, tz)
    start, end = to_bounds(first, last, tz)
    orders = fetch_orders(session, url, start, end)
    metrics = compute(orders)

    payload: dict[str, Any] = {
        "status": STATUS_OK if orders else STATUS_NO_DATA,
        "period": {
            "name": period,
            "label": label,
            "date_from": first.isoformat(),
            "date_to": last.isoformat(),
            "timezone": str(tz),
            "utc_from": utc_str(start),
            "utc_to": utc_str(end),
        },
        "metrics": metrics,
        "comparison": None,
        "notes": [
            "Refunds are attributed to the parent order date, not the refund date.",
            "Revenue excludes cancelled, failed and refunded orders.",
        ],
        "error": None,
    }

    if compare:
        span = (last - first).days + 1
        prev_last = first - timedelta(days=1)
        prev_first = prev_last - timedelta(days=span - 1)
        prev_start, prev_end = to_bounds(prev_first, prev_last, tz)
        prev_metrics = compute(fetch_orders(session, url, prev_start, prev_end))
        payload["comparison"] = {
            "date_from": prev_first.isoformat(),
            "date_to": prev_last.isoformat(),
            "metrics": prev_metrics,
            "deltas": {
                "orders_count": delta(
                    metrics["orders_count"], prev_metrics["orders_count"]
                ),
                "revenue_gross": delta(
                    metrics["revenue_gross"], prev_metrics["revenue_gross"]
                ),
                "revenue_net": delta(
                    metrics["revenue_net"], prev_metrics["revenue_net"]
                ),
                "aov": delta(metrics["aov"], prev_metrics["aov"]),
            },
        }

    return payload


def render(payload: dict[str, Any]) -> None:
    """Print a human-readable summary to stderr."""
    out = sys.stderr
    period = payload["period"]
    print("=" * 58, file=out)
    print(
        f"{period['label']}  ({period['date_from']} .. {period['date_to']})", file=out
    )
    print(f"status: {payload['status']}   tz: {period['timezone']}", file=out)
    print(f"utc   : {period['utc_from']} .. {period['utc_to']}", file=out)
    print("=" * 58, file=out)

    metrics = payload["metrics"]
    currency = metrics["currency"] or ""
    print(f"orders           : {metrics['orders_count']}", file=out)
    print(f"revenue orders   : {metrics['revenue_orders_count']}", file=out)
    print(f"revenue gross    : {metrics['revenue_gross']} {currency}", file=out)
    print(f"refunds          : {metrics['refunds_total']} {currency}", file=out)
    print(f"revenue net      : {metrics['revenue_net']} {currency}", file=out)
    print(f"aov              : {metrics['aov']} {currency}", file=out)

    if metrics["by_status"]:
        print(file=out)
        print(f"{'status':<12}{'count':>7}{'total':>12}", file=out)
        print("-" * 31, file=out)
        for status, bucket in metrics["by_status"].items():
            print(f"{status:<12}{bucket['count']:>7}{bucket['total']:>12.2f}", file=out)

    comparison = payload["comparison"]
    if comparison:
        print(file=out)
        print(f"vs {comparison['date_from']} .. {comparison['date_to']}", file=out)
        print("-" * 31, file=out)
        for name, change in comparison["deltas"].items():
            percent = change["percent"]
            suffix = "n/a" if percent is None else f"{percent:+.1f}%"
            print(f"{name:<18}{change['absolute']!s:>10}  {suffix}", file=out)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Order metrics for a period.")
    parser.add_argument(
        "--period", default="today", help=f"one of: {', '.join(PERIODS)}"
    )
    parser.add_argument("--date-from", default=None, help="YYYY-MM-DD, custom only")
    parser.add_argument("--date-to", default=None, help="YYYY-MM-DD, custom only")
    parser.add_argument(
        "--no-compare", action="store_true", help="skip the previous period"
    )
    parser.add_argument(
        "--json", action="store_true", help="print JSON only, no summary"
    )
    return parser.parse_args()


def main() -> int:
    """Entry point. Returns a process exit code."""
    args = parse_args()
    load_dotenv(ENV_PATH)

    try:
        store_url = env("WOO_STORE_URL").rstrip("/")
        tz = ZoneInfo(env("BUSINESS_TIMEZONE"))
        session = requests.Session()
        session.auth = (env("WOO_CONSUMER_KEY"), env("WOO_CONSUMER_SECRET"))
        payload = build(
            session,
            f"{store_url}/wp-json/wc/v3/orders",
            tz,
            args.period,
            args.date_from,
            args.date_to,
            not args.no_compare,
        )
    except ToolError as exc:
        payload = {
            "status": exc.status,
            "period": None,
            "metrics": None,
            "comparison": None,
            "notes": [],
            "error": {"code": exc.code, "message": exc.message},
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 2

    if not args.json:
        render(payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
