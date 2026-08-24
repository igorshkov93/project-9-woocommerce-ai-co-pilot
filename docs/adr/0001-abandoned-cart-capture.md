# ADR 0001: Abandoned cart capture via a custom mu-plugin

- Status: Accepted
- Date: 2026-08-21
- Scope: Project #9 — WooCommerce AI Co-pilot, Step 8

## Context

One of the five bot capabilities required by the brief is drafting an email
campaign for abandoned carts. WooCommerce does not store abandoned carts.

What core WooCommerce actually provides:

- `wp_woocommerce_sessions` — a key/value table holding a PHP-serialized blob
  per active session (`cart`, `customer`, `cart_totals`). It is purged by the
  `woocommerce_cleanup_sessions` cron job, default lifetime 48 hours.
- No REST endpoint exposing cart contents for a third party.
- No notion of "abandoned" and no notion of "recovered".

Abandonment is a derived state: a cart is abandoned if no order was placed and
the cart has not changed for longer than a threshold. It depends on when the
question is asked, so it cannot be a stored flag.

The AI tool `draft_abandoned_cart_email` (Step 14) must operate on real store
data. Without a capture layer it would generate plausible text unrelated to the
catalog, which defeats the purpose of the project.

## Decision

Ship a must-use plugin, kept as a file in the repository under `wp-plugins/`,
that does three things:

1. Writes a snapshot of the cart into a dedicated table on every cart change.
2. Marks a snapshot as recovered when an order is created from that session.
3. Exposes a read-only REST route that computes abandonment at request time.

### Storage

Table: `{$wpdb->prefix}copilot_abandoned_carts`, created through `dbDelta()`,
schema version tracked in the `copilot_abandoned_carts_db_version` option.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED AUTO_INCREMENT | primary key |
| `session_key` | VARCHAR(64) | unique; WooCommerce session id |
| `user_id` | BIGINT UNSIGNED | 0 for guests |
| `email` | VARCHAR(190) | captured opportunistically, may be empty |
| `first_name` | VARCHAR(100) | used for email personalization |
| `currency` | CHAR(3) | frozen at snapshot time |
| `items_count` | SMALLINT UNSIGNED | sum of quantities |
| `cart_total` | DECIMAL(12,2) | frozen at snapshot time |
| `items` | LONGTEXT | JSON array, see below |
| `created_gmt` | DATETIME | first time this session had a cart |
| `updated_gmt` | DATETIME | last cart change; drives the threshold |
| `recovered_order_id` | BIGINT UNSIGNED | 0 while not recovered |
| `recovered_gmt` | DATETIME NULL | set together with the order id |

Indexes: unique on `session_key`, plain on `updated_gmt` and
`recovered_order_id`.

Each element of `items` is:

```json
{
  "product_id": 2864,
  "sku": "ELEC-EARBUD-01",
  "name": "Wireless Earbuds Pro",
  "quantity": 2,
  "price": 79.00,
  "line_total": 158.00
}
```

Prices are frozen at snapshot time. A recovery email sent days later must not
quote a total the customer never saw.

### REST contract

`GET /wp-json/copilot/v1/abandoned-carts`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `threshold_minutes` | integer, 5..10080 | 60 | minimum idle time |
| `require_email` | boolean | true | drop carts with no contact address |
| `limit` | integer, 1..100 | 20 | page size |
| `include_recovered` | boolean | false | diagnostics only |

Response:

```json
{
  "generated_at_gmt": "2026-08-21T09:12:00Z",
  "threshold_minutes": 60,
  "currency": "USD",
  "count": 7,
  "carts": [
    {
      "id": 12,
      "email": "buyer@example.com",
      "first_name": "Anna",
      "items_count": 3,
      "cart_total": 237.00,
      "currency": "USD",
      "items": [],
      "updated_gmt": "2026-08-21T04:41:00Z",
      "updated_local": "2026-08-21T07:41:00+03:00",
      "minutes_since_update": 271,
      "recovered": false
    }
  ]
}
```

`updated_local` is rendered in the store timezone, which is the same source of
truth used everywhere else in this project (`BUSINESS_TIMEZONE`). The LLM never
sees raw UTC timestamps: it would silently reason about the wrong day.

### Authentication

WordPress Application Passwords over HTTPS (HTTP Basic), plus a
`permission_callback` requiring the `manage_woocommerce` capability.

WooCommerce consumer key/secret are not an option: that authentication scheme
only applies inside the `wc/v3` namespace, not a custom one.

### Why must-use and not a regular plugin

- It cannot be deactivated from the admin UI, so a live demo cannot start
  returning 404 because someone toggled a checkbox.
- It loads before regular plugins, so the capture hooks are always registered.
- Trade-off: mu-plugins have no activation hook, so table creation runs on
  `plugins_loaded` guarded by a stored schema version.
- Files in subdirectories of `mu-plugins/` are not auto-loaded, so a one-line
  loader lives at the root of `mu-plugins/` and requires the real file.

## Alternatives considered

**Read `wp_woocommerce_sessions` directly at query time.** Rejected: rows are
purged after 48 hours, so history disappears; `session_value` is a PHP
serialization that is fragile to parse from outside PHP; and a session carries
no record of whether an order was eventually placed.

**Store the code in the Code Snippets plugin.** Rejected: snippets live in the
database, so they are invisible to git, cannot be reviewed by a recruiter, and
are not reproducible on a fresh install. Code Snippets was deliberately
deactivated in Step 5.

**A shared secret in a custom header.** Rejected: it reimplements
authentication that WordPress already ships, and would have to be rotated and
stored by hand. Application Passwords map directly onto n8n's Basic Auth
credential type.

**An off-the-shelf abandoned cart plugin.** Rejected: the engineering value of
this project is the integration layer, and a black box would remove the part
worth showing.

## Consequences

- Positive: abandoned carts become a first-class, queryable resource with a
  stable JSON contract; the AI tool layer stays provider-agnostic and only
  consumes JSON.
- Positive: the capture layer is a file in the repository, reviewable and
  reproducible.
- Negative: this is custom code in the store, and it must be maintained
  alongside WooCommerce cart hooks, which differ between the classic checkout
  and the Store API (blocks). Both are handled.
- Negative: snapshots accumulate. No automatic pruning is implemented in this
  version; retention is documented as known debt.
- Local stand only: the site uses a self-signed certificate, so n8n HTTP nodes
  disable SSL verification. Acceptable for a local demo, unacceptable in
  production; noted in the architecture documentation.