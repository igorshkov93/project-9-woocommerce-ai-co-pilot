# ADR 0004: Order summary metrics, windows and refund attribution

Date: 2026-08-26
Status: Accepted

## Context

The `get_orders_summary` tool answers "how many orders today?" and similar
questions. Three decisions had no obvious right answer and are recorded here
because they shape every number the bot reports.

The store runs WooCommerce with HPOS enabled, in `Europe/Kyiv`, in USD, with
239 seeded orders across 60 days and 22 refunds.

## Decision 1: windows are computed in the business timezone, queried in UTC

Day boundaries are resolved as `[00:00:00, next 00:00:00)` in
`BUSINESS_TIMEZONE`, converted to UTC, and sent with `dates_are_gmt=true` so
the filter applies to `date_created_gmt`.

Probing confirmed the flag changes interpretation: the same bound string
returned 1 order as UTC and 2 as site-local. Relying on site-local dates would
make results depend on a WordPress setting the tool does not control.

`after` and `before` are strictly exclusive in WP_Date_Query. Probing showed
an order is excluded when `after` equals its exact timestamp and included one
second earlier, so the lower bound is shifted back one second. Without the
shift, an order created exactly at midnight would vanish.

## Decision 2: revenue excludes cancelled, failed and refunded orders

Gross revenue sums `pending`, `processing`, `on-hold` and `completed`.

`pending` and `on-hold` are included even though the money has not settled:
the store owner asking "how are we doing today?" wants committed orders, not
only cleared payments. The full status breakdown is returned alongside, so a
follow-up question about unpaid orders needs no second call.

## Decision 3: refunds are attributed to the parent order date

The orders endpoint returns a `refunds` array containing `id`, `reason`,
`total` and `total_tax` — but no date. Precise attribution would require
`GET /orders/<id>/refunds` per order, turning one request into N+1.

A second reason settled it: in the seeded data every refund carries
`date_created_gmt` of the seeding day, drifting 33 to 59 days from its parent
order. Refund dates are not trustworthy here, so attributing by refund date
would be precise about a meaningless number.

Consequence: a refund issued today against a month-old order does not reduce
today's net revenue. This is stated in the tool's `notes` field so the model
can qualify its answer.

## Decision 4: refunds are only subtracted from orders that entered revenue

An order with status `refunded` is already excluded from gross. Subtracting
its refund total as well would deduct the same money twice.

The measured difference is real, not theoretical: over the 90-day window the
status-aware total is 595 while the naive total across all orders is higher.
The verification suite computes both and fails if the tool matches the naive
one, so the guard cannot silently regress.

## Decision 5: the n8n implementation is validated against a Python reference

`scripts/orders_summary.py` is the reference; the n8n sub-workflow must agree
to the cent. Both were compared across three windows:

| Window | Orders | Gross | Refunds | AOV |
|---|---|---|---|---|
| 2026-08-08 .. 08-14 | 31 | 3053.00 | 264.00 | 109.04 |
| 2026-07-15 .. 08-21 | 166 | 12937.00 | 595.00 | 89.84 |
| 2026-08-24 .. 08-25 | 0 | — | — | `no_data` |

The 166-order window spans two API pages and validates pagination. The empty
window validates the `no_data` path. Invalid input was checked separately and
returns identical error codes in both implementations.

## Contract

`status` is one of `ok`, `no_data`, `invalid_input`, `upstream_error`, mirroring
the three-status contract of `resolve_entity` (ADR 0003). Failures return the
same key set as successes with `metrics` and `period` set to null, so the agent
never branches on response shape.

Input validation rejects unknown period names, `custom` without dates,
malformed or impossible dates, inverted ranges, and windows longer than 92 days.
The 92-day cap bounds pagination: at roughly five orders per day it stays
within the 50-page limit configured on the HTTP nodes.

## Consequences

- Refund timing is approximate by design; documented in `notes`.
- Changing `REVENUE_STATUSES` invalidates the measured figures above and
  requires re-running `verify_orders_summary.py`.
- The tool is read-only and safe to call repeatedly.

## Known limitations

- Seeded refund dates are unusable; the top-up generator must backdate refunds
  as well as orders.
- Refund reasons are available but not aggregated; that belongs to Step 12
  (`get_top_returned_products`).