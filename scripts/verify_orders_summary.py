"""Verification tests for orders_summary.py.

This is a test, not a report: it exits with code 1 when any check fails,
so it can gate a commit or a CI run.

Windows are anchored to the newest order present in the store rather than
to the current date, so the suite stays green as data ages and after the
top-up generator adds fresh orders.

Usage:
    python verify_orders_summary.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import urllib3
from dotenv import load_dotenv

import orders_summary as tool

SCRIPT_PATH = Path(__file__).resolve().parent / "orders_summary.py"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
TOLERANCE = 0.005
ANCHOR_SPAN_DAYS = 14
WIDE_SPAN_DAYS = 90

_results: list[tuple[bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> bool:
    """Record and print a single assertion."""
    _results.append((passed, name))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    if detail and not passed:
        print(f"       {detail}")
    return passed


def close(left: float | None, right: float | None) -> bool:
    """Compare two monetary values with a half-cent tolerance."""
    if left is None or right is None:
        return left is right
    return abs(left - right) < TOLERANCE


def run_cli(args: list[str]) -> tuple[int, dict[str, Any]]:
    """Run the tool as a subprocess and parse its JSON output."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--json", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(SCRIPT_PATH.parent),
        check=False,
    )
    try:
        payload: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"       non-JSON stdout: {proc.stdout[:200]!r}")
        payload = {}
    return proc.returncode, payload


def make_session() -> requests.Session:
    """Build an authenticated session mirroring the tool's own settings."""
    session = requests.Session()
    session.auth = (tool.env("WOO_CONSUMER_KEY"), tool.env("WOO_CONSUMER_SECRET"))
    if not tool.tls_verify():
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def edge_order_day(
    session: requests.Session, url: str, tz: ZoneInfo, newest: bool
) -> date:
    """Return the local day of the newest or oldest order in the store."""
    response = session.get(
        url,
        params={
            "per_page": 1,
            "status": ",".join(tool.ALL_STATUSES),
            "orderby": "date",
            "order": "desc" if newest else "asc",
        },
        timeout=tool.TIMEOUT,
        verify=tool.tls_verify(),
    )
    response.raise_for_status()
    orders: list[dict[str, Any]] = response.json()
    if not orders:
        print("[FATAL] store has no orders at all")
        sys.exit(1)
    stamp = tool.parse_gmt(orders[0]["date_created_gmt"])
    return stamp.astimezone(tz).date()


def recompute(orders: list[dict[str, Any]]) -> dict[str, float | int]:
    """Independent metric computation, deliberately written differently.

    Also produces the naive refund variant that ignores order status, used
    to prove the double-subtraction guard is actually engaged.
    """
    revenue = [o for o in orders if o["status"] in tool.REVENUE_STATUSES]
    gross = sum(float(o["total"]) for o in revenue)
    refunds_guarded = sum(
        abs(float(r["total"])) for o in revenue for r in (o.get("refunds") or [])
    )
    refunds_naive = sum(
        abs(float(r["total"])) for o in orders for r in (o.get("refunds") or [])
    )
    return {
        "orders_count": len(orders),
        "revenue_orders_count": len(revenue),
        "revenue_gross": round(gross, 2),
        "refunds_guarded": round(refunds_guarded, 2),
        "refunds_naive": round(refunds_naive, 2),
        "revenue_net": round(gross - refunds_guarded, 2),
    }


def fetch_window(
    session: requests.Session, url: str, first: date, last: date, tz: ZoneInfo
) -> list[dict[str, Any]]:
    """Fetch raw orders for an inclusive local day range."""
    start, end = tool.to_bounds(first, last, tz)
    return tool.fetch_orders(session, url, start, end)


def test_validation() -> None:
    """Malformed input must produce invalid_input and exit code 2."""
    print()
    print("--- input validation ---")
    cases = [
        (["--period", "nonsense"], "bad_period", "unknown period name"),
        (["--period", "custom"], "missing_dates", "custom without dates"),
        (
            [
                "--period",
                "custom",
                "--date-from",
                "2026-08-32",
                "--date-to",
                "2026-08-21",
            ],
            "bad_date",
            "impossible calendar date",
        ),
        (
            [
                "--period",
                "custom",
                "--date-from",
                "2026-08-21",
                "--date-to",
                "2026-08-15",
            ],
            "inverted_range",
            "date_to before date_from",
        ),
        (
            [
                "--period",
                "custom",
                "--date-from",
                "2026-01-01",
                "--date-to",
                "2026-12-31",
            ],
            "window_too_large",
            "window beyond the page-count guard",
        ),
    ]
    for args, expected_code, label in cases:
        code, payload = run_cli(args)
        actual = (payload.get("error") or {}).get("code")
        check(
            f"validation: {label} -> {expected_code}",
            payload.get("status") == tool.STATUS_INVALID
            and actual == expected_code
            and code == 2,
            f"status={payload.get('status')} code={actual} rc={code}",
        )
        check(
            f"validation: {label} nulls metrics",
            payload.get("metrics") is None and payload.get("period") is None,
        )


def test_window_semantics(tz: ZoneInfo, anchor_first: date, anchor_last: date) -> None:
    """Period resolution, UTC conversion and comparison alignment."""
    print()
    print("--- window semantics ---")
    code, payload = run_cli(
        [
            "--period",
            "custom",
            "--date-from",
            anchor_first.isoformat(),
            "--date-to",
            anchor_last.isoformat(),
        ]
    )
    check("anchor window: exit code 0", code == 0, f"rc={code}")
    check(
        "anchor window: status ok",
        payload.get("status") == tool.STATUS_OK,
        f"status={payload.get('status')}",
    )

    period = payload.get("period") or {}
    check(
        "anchor window: dates echoed back",
        period.get("date_from") == anchor_first.isoformat()
        and period.get("date_to") == anchor_last.isoformat(),
        f"got {period.get('date_from')} .. {period.get('date_to')}",
    )
    check(
        "anchor window: timezone reported",
        period.get("timezone") == str(tz),
        f"got {period.get('timezone')}",
    )

    start, end = tool.to_bounds(anchor_first, anchor_last, tz)
    check(
        "anchor window: local midnight converted to UTC",
        period.get("utc_from") == tool.utc_str(start)
        and period.get("utc_to") == tool.utc_str(end),
        f"got {period.get('utc_from')} .. {period.get('utc_to')}",
    )

    comparison = payload.get("comparison") or {}
    prev_last = date.fromisoformat(comparison["date_to"])
    prev_first = date.fromisoformat(comparison["date_from"])
    span = (anchor_last - anchor_first).days
    check(
        "comparison: ends the day before the window starts",
        prev_last == anchor_first - timedelta(days=1),
        f"got {prev_last}",
    )
    check(
        "comparison: same length as the window",
        (prev_last - prev_first).days == span,
        f"{(prev_last - prev_first).days} vs {span}",
    )

    _, no_compare = run_cli(
        [
            "--period",
            "custom",
            "--date-from",
            anchor_first.isoformat(),
            "--date-to",
            anchor_last.isoformat(),
            "--no-compare",
        ]
    )
    check(
        "comparison: suppressed by --no-compare",
        no_compare.get("comparison") is None,
    )


def test_internal_consistency(anchor_first: date, anchor_last: date) -> None:
    """Metrics must agree with their own by_status breakdown."""
    print()
    print("--- internal consistency ---")
    _, payload = run_cli(
        [
            "--period",
            "custom",
            "--date-from",
            anchor_first.isoformat(),
            "--date-to",
            anchor_last.isoformat(),
            "--no-compare",
        ]
    )
    metrics = payload["metrics"]
    buckets: dict[str, dict[str, Any]] = metrics["by_status"]

    check(
        "orders_count equals the sum of by_status counts",
        metrics["orders_count"] == sum(b["count"] for b in buckets.values()),
    )
    revenue_count = sum(
        b["count"] for s, b in buckets.items() if s in tool.REVENUE_STATUSES
    )
    check(
        "revenue_orders_count equals revenue-status counts",
        metrics["revenue_orders_count"] == revenue_count,
        f"{metrics['revenue_orders_count']} vs {revenue_count}",
    )
    revenue_total = sum(
        b["total"] for s, b in buckets.items() if s in tool.REVENUE_STATUSES
    )
    check(
        "revenue_gross equals revenue-status totals",
        close(metrics["revenue_gross"], revenue_total),
        f"{metrics['revenue_gross']} vs {revenue_total}",
    )
    check(
        "revenue_net equals gross minus refunds",
        close(
            metrics["revenue_net"],
            metrics["revenue_gross"] - metrics["refunds_total"],
        ),
    )
    expected_aov = (
        round(metrics["revenue_gross"] / revenue_count, 2) if revenue_count else None
    )
    check(
        "aov equals gross divided by revenue orders",
        close(metrics["aov"], expected_aov),
        f"{metrics['aov']} vs {expected_aov}",
    )
    check(
        "excluded statuses never enter revenue",
        all(s in tool.ALL_STATUSES for s in buckets),
        f"unexpected statuses: {sorted(set(buckets) - set(tool.ALL_STATUSES))}",
    )


def test_partition(
    session: requests.Session,
    url: str,
    tz: ZoneInfo,
    anchor_last: date,
) -> None:
    """Two adjacent single-day windows must sum to the two-day window."""
    print()
    print("--- window partition (boundary integrity) ---")
    day_b = anchor_last
    day_a = anchor_last - timedelta(days=1)

    def totals(first: date, last: date) -> dict[str, Any]:
        _, payload = run_cli(
            [
                "--period",
                "custom",
                "--date-from",
                first.isoformat(),
                "--date-to",
                last.isoformat(),
                "--no-compare",
            ]
        )
        return dict(payload["metrics"])

    left = totals(day_a, day_a)
    right = totals(day_b, day_b)
    both = totals(day_a, day_b)

    check(
        "order counts partition exactly",
        left["orders_count"] + right["orders_count"] == both["orders_count"],
        f"{left['orders_count']} + {right['orders_count']} != {both['orders_count']}",
    )
    check(
        "gross revenue partitions exactly",
        close(left["revenue_gross"] + right["revenue_gross"], both["revenue_gross"]),
        f"{left['revenue_gross']} + {right['revenue_gross']} "
        f"!= {both['revenue_gross']}",
    )
    check(
        "refunds partition exactly",
        close(left["refunds_total"] + right["refunds_total"], both["refunds_total"]),
    )
    raw = fetch_window(session, url, day_a, day_b, tz)
    check(
        "no duplicate order ids across pagination",
        len({o["id"] for o in raw}) == len(raw),
        f"{len(raw)} orders, {len({o['id'] for o in raw})} unique",
    )


def test_against_independent_recompute(
    session: requests.Session,
    url: str,
    tz: ZoneInfo,
    wide_first: date,
    wide_last: date,
) -> None:
    """Compare the tool against a second implementation over a wide window."""
    print()
    print("--- independent recomputation and refund guard ---")
    _, payload = run_cli(
        [
            "--period",
            "custom",
            "--date-from",
            wide_first.isoformat(),
            "--date-to",
            wide_last.isoformat(),
            "--no-compare",
        ]
    )
    metrics = payload["metrics"]
    expected = recompute(fetch_window(session, url, wide_first, wide_last, tz))

    check(
        "recompute: orders_count",
        metrics["orders_count"] == expected["orders_count"],
        f"{metrics['orders_count']} vs {expected['orders_count']}",
    )
    check(
        "recompute: revenue_gross",
        close(metrics["revenue_gross"], float(expected["revenue_gross"])),
        f"{metrics['revenue_gross']} vs {expected['revenue_gross']}",
    )
    check(
        "recompute: revenue_net",
        close(metrics["revenue_net"], float(expected["revenue_net"])),
        f"{metrics['revenue_net']} vs {expected['revenue_net']}",
    )
    check(
        "refund guard: matches status-aware total",
        close(metrics["refunds_total"], float(expected["refunds_guarded"])),
        f"{metrics['refunds_total']} vs {expected['refunds_guarded']}",
    )

    guarded = float(expected["refunds_guarded"])
    naive = float(expected["refunds_naive"])
    if close(guarded, naive):
        check(
            "refund guard: window contains a refunded-status order",
            False,
            "guarded and naive totals are identical - this window cannot "
            "prove the guard works. Widen WIDE_SPAN_DAYS.",
        )
    else:
        check(
            "refund guard: excludes refunded-status orders",
            not close(metrics["refunds_total"], naive),
            f"tool={metrics['refunds_total']} naive={naive}",
        )


def main() -> int:
    """Run every check and return a process exit code."""
    load_dotenv(ENV_PATH)
    tz = ZoneInfo(tool.env("BUSINESS_TIMEZONE"))
    url = f"{tool.env('WOO_STORE_URL').rstrip('/')}/wp-json/wc/v3/orders"
    session = make_session()

    newest = edge_order_day(session, url, tz, newest=True)
    oldest = edge_order_day(session, url, tz, newest=False)
    anchor_first = newest - timedelta(days=ANCHOR_SPAN_DAYS - 1)
    wide_first = max(oldest, newest - timedelta(days=WIDE_SPAN_DAYS - 1))

    print("=" * 62)
    print("verify_orders_summary")
    print("=" * 62)
    print(f"timezone      : {tz}")
    print(f"oldest order  : {oldest.isoformat()}")
    print(f"newest order  : {newest.isoformat()}")
    print(f"anchor window : {anchor_first.isoformat()} .. {newest.isoformat()}")
    print(f"wide window   : {wide_first.isoformat()} .. {newest.isoformat()}")

    test_validation()
    test_window_semantics(tz, anchor_first, newest)
    test_internal_consistency(anchor_first, newest)
    test_partition(session, url, tz, newest)
    test_against_independent_recompute(session, url, tz, wide_first, newest)

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print()
    print("=" * 62)
    print(f"{passed}/{total} checks passed")
    if passed != total:
        print()
        print("failed checks:")
        for ok, name in _results:
            if not ok:
                print(f"  - {name}")
        return 1
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
