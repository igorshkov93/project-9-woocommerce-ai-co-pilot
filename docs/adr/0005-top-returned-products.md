# ADR 0005: Counting returns for get_top_returned_products

Date: 2026-08-26
Status: Accepted

## Context

The tool answers "top 5 problem products by returns" for the store owner
via Telegram. Three properties of the WooCommerce REST API and of the
seeded dataset forced decisions that are not obvious from the code alone.

## Decision 1: the N+1 detail call is unavoidable

The `refunds` array embedded in `GET /orders` carries only `id`, `reason`,
`total` and `total_tax`. It has no `line_items`, so it cannot answer which
product came back — which is the entire point of the tool.

Measured on the live store: 240 orders, 22 of which carry a non-empty
refunds array. The embedded array is therefore used as a cheap filter and
the detail call `GET /orders/<id>/refunds` is issued only for those orders.
N is bound to the number of refunds, not to the number of orders.

Rejected alternative: the WooCommerce Analytics REST namespace exposes
aggregated report endpoints, but they are not part of the documented
`wc/v3` contract and their shape has changed between minor releases.

Note: the detail endpoint names a refund's total `amount`, while the
embedded array names the same value `total`. Reading the wrong key yields
a silent zero. This was caught by a probe, not by review.

## Decision 2: two different denominators, deliberately

`orders_summary.py` (Step 11) excludes `refunded` orders from revenue: the
money left the business, and subtracting the refund again would deduct it
twice. This tool includes `refunded` in `SOLD_STATUSES` instead.

The reason is that the two tools answer different questions. Revenue asks
what the business earned. Returns ask what physically came back — and a
unit was sold before it was returned. Dropping `refunded` from the
denominator would push `return_rate_pct` above 100 percent.

The gap was measured rather than assumed, over the window
2026-07-15 .. 2026-08-21:

| parent order status | refunds | amount |
|---|---|---|
| completed (partial refunds) | 11 | 595.00 |
| refunded (full refunds)     |  4 | 162.00 |
| total                       | 15 | 757.00 |

595.00 is the figure `get_orders_summary` reports. 757.00 is the figure
this tool reports. Both are correct. If they matched, one would be wrong.

Cross-check: `_refund_type` in refund `meta_data` splits 4 full / 11
partial, which agrees with the parent order statuses independently.

## Decision 3: attribution by parent order date

Every seeded refund carries `date_created_gmt = 2026-08-21`, the day the
data was generated, with a drift of 33 to 59 days from its parent order.
Attributing by refund date would return either everything or nothing for
any realistic window.

Refunds are therefore attributed to the window by the date of the parent
order. The mode is surfaced in the contract as
`window.attribution = "order_date"` rather than hidden, so a future switch
to refund-date attribution against real data is a visible change.

## Decision 4: rank by money, not by units

With 15 refunds spread over 10 products, unit counts are almost all 1 or 2
and produce ties nearly everywhere, making the ranking arbitrary. Refunded
money is continuous, breaks ties naturally, and is the figure a store owner
acts on. Units and return rate are returned as context.

Ties are broken by units returned, then by product id, so the ranking is
deterministic across runs and across the two implementations.

## Decision 5: integer cents in both runtimes

Money is accumulated as integer cents in Python and in the n8n Code node.
`Decimal` does not exist in JavaScript, and float accumulation drifts
differently in the two runtimes, so a to-the-cent agreement would be luck
rather than a guarantee. `round(value * 100)` over integers reproduces
exactly on both sides.

## Contract

status: ok | no_data | invalid_input | upstream_error
window: from, to, timezone, attribution, orders_scanned
totals: refunds_count, orders_affected, refund_amount, units_returned,
full_refunds, partial_refunds, products_affected, reasons
diagnostics: detail_calls, excluded_by_status, non_product_lines,
amount_mismatches
top[]: rank, product_id, sku, name, units_returned, refund_amount,
orders_affected, units_sold, return_rate_pct, rate_significant,
top_reason, reasons
limit this mirrors `get_orders_summary` so the agent sees one error shape across
tools.

## Verification

`scripts/verify_top_returned.py` runs 47 assertions, all green. Totals are
cross-checked against the embedded refunds array, which the implementation
never reads, so a shared bug is less likely to cancel itself out.

The load-bearing assertion is `units_returned <= units_sold` per product:
it fails loudly if anyone later "unifies" `SOLD_STATUSES` with the revenue
statuses.

Python and n8n agree exactly on the reference window, including the order
of tied reason counts.

## Known limitations

- `top_reason` is arbitrary under a full tie. Product 2864 has three
  refunds with three distinct reasons, so the field is decided
  alphabetically. The complete `reasons` map is returned alongside it and
  is what the agent should reason over.
- `rate_significant` is false below 3 units sold; a single sale returned
  once would otherwise read as a 100 percent return rate.
- Python serialises whole amounts as `757.0` and JavaScript as `757`. These
  are equal as JSON numbers, but a future verify script must compare
  numerically, not as strings.
- The tool re-fetches all orders in the window on every call. Acceptable at
  this data volume; a cache would be the first optimisation if the store
  grew.