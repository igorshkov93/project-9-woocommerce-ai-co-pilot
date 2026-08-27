# ADR 0006: create_coupon tool — state, idempotency, category expansion

## Status
Accepted

## Context

Step 13 introduces the first WooCommerce Co-pilot tool that writes to the
store, in response to "Создай новый купон −15% на категорию Электроника".
All prior tools (09–12) were read-only. Writing changes the risk profile:
a mistaken or duplicated call creates a real coupon in the store, not just
a wrong chat reply.

The tool is invoked through a Telegram inline-keyboard confirmation flow.
n8n sub-workflows have no memory between invocations, so a naive design
would need external state to bridge "bot proposed a coupon" and "owner
pressed Confirm".

## Decisions

### 1. State between propose and confirm

No external state store (DB table, custom REST endpoint) is introduced.
The confirmation payload is encoded directly in the Telegram inline
button's `callback_data`:

    cc|<category_id>|<discount_percent>|<expires_days>

Example: `cc|67|15|14` (~12 bytes, well under Telegram's 64-byte limit
for `callback_data`).

Category descendant resolution (see Decision 3) is deferred to the
confirm step, not done at propose time, so only cheap identifiers travel
in the button — never a resolved product/category list.

Consequence: propose and confirm are two separate sub-workflows
(`13a-tool-propose-coupon`, `13b-tool-confirm-coupon`) triggered from two
different places — the future AI Agent (Step 15) calls propose as a tool;
a new `callback_query` branch in `09-telegram-gateway` routes confirm.

### 2. Idempotency on double-press

Two independent layers:

- UX layer: on receiving the callback, immediately `answerCallbackQuery`
  and edit the message to remove/replace the inline keyboard, so a second
  tap has nothing to press in most cases.
- API layer (authoritative — the UX layer is not trusted alone, per the
  fail-closed precedent in ADR 0002): the coupon code is deterministic,
  derived from inputs: `ELEC-15-<YYYYMMDD>` (creation date). Before
  `POST /coupons`, the confirm sub-workflow runs `GET /coupons?code=...`.
  If found, it returns `duplicate` with the existing coupon's id instead
  of creating a second one.

### 3. Category → descendants expansion

Confirmed empirically (probe_category_children.py, 2026-08-27):

- Electronics (id=67): flat, no children, count=6 (matches the known 6
  ELEC-* products).
- Clothing (id=16): 32 descendants across 5 levels (WooCommerce sample
  taxonomy: Men/Women/Tops/Bottoms/Collections/Promotions), leaf counts
  5+3+5=13 plus 1 product directly on Clothing = 14, matching Clothing's
  own `count` field exactly. Recursive traversal reproduces this
  correctly.

Decision: expand the target category into the full list of descendant
category ids (root + all levels down) and pass that array into the
coupon's `product_categories` field, rather than resolving to a product
list. This fixes the "coupon on parent category doesn't apply to
children" bug directly at the category level, stays correct as the
catalog changes (no product list to keep in sync), and stays small even
in the worst observed case (32 ids for Clothing). Empty descendant
categories are included without filtering — applying a coupon to an
empty category has no functional effect, so filtering by `count > 0`
would only be a cosmetic simplification, not a correctness requirement.

### 4. Coupon field defaults

Required by the WooCommerce REST API: `code`, `discount_type` (`percent`),
`amount` (string, e.g. `"15.00"`).

Defaults applied by the tool (not requested explicitly in the task, but
necessary for the coupon to behave predictably in a demo and to survive
an interview question about them):

| field                    | value                        | rationale                                  |
|--------------------------|-------------------------------|---------------------------------------------|
| `date_expires`           | +14 days from creation        | a promo should not run forever silently     |
| `individual_use`         | `true`                        | does not stack with other coupons           |
| `exclude_sale_items`     | `true`                        | avoids double-discounting already-reduced items |
| `usage_limit_per_user`   | `1`                            | limits abuse of a single shared code        |
| `usage_limit` (total)    | unset (unlimited)             | this is a category-wide promo, not a personal code |
| `product_categories`     | `[category_id, ...descendants]` | see Decision 3                            |

### 5. Dry-run semantics for a write tool

Prior tools' "dry-run" meant "never call the API". That does not apply
literally to a tool whose purpose is to create something. Instead: the
**propose** phase IS the dry-run — it resolves categories, builds the full
coupon payload, and checks for an existing duplicate via `GET /coupons`,
but stops before `POST`. The **confirm** phase is the only place that
calls `POST /coupons`, equivalent to the `--apply` flag used by the
project's Python scripts.

### 6. Contract

Two contracts, matching the `status + payload` shape used since ADR 0004:

`propose_coupon`: `proposed | invalid_input | no_data | upstream_error`
(`no_data` = category not found, or resolves to zero categories)

`confirm_coupon`: `created | duplicate | invalid_input | upstream_error`

## Consequences

- No new infrastructure (no pending-actions table/endpoint) is needed for
  confirmation state; it rides entirely on Telegram's own callback_data
  round-trip.
- The category-expansion code path is exercised against a real
  multi-level case (Clothing) even though the Step 13 target (Electronics)
  is flat — de-risks correctness before Step 15 wiring.
- Deterministic coupon codes mean a second `create_coupon` request for
  the same category/discount/day is always caught as `duplicate`, at the
  cost of not being able to create two distinct 15%-Electronics coupons
  on the same calendar day on purpose. Acceptable for this tool's scope.

  
## Addendum (2026-08-27, post end-to-end verification)

- WooCommerce normalizes coupon codes to lowercase on save
  (`wc_format_coupon_code()`). The API response's top-level `code` field
  comes back lowercase (`electronics-15-20260827`) even though the tool
  submits it uppercase (`ELECTRONICS-15-20260827`, preserved in `payload`
  as sent). This is expected WooCommerce behavior, not a bug in this tool.
- The duplicate-detection query (`GET /coupons?code=<uppercase>`) matches
  the lowercase-stored record only because MySQL's default collation is
  case-insensitive. Verified empirically twice: the Music test coupon in
  verify_create_coupon.py (Step 13.4) and the real Electronics coupon
  end-to-end (Step 13.7, second confirm correctly returned `duplicate`
  with the same `coupon_id`). This is a soft dependency on collation, not
  an explicit normalization in the tool's own logic — flagged as a Step 16
  candidate (lowercase the code before comparing, to not rely on DB
  collation).
- End-to-end verified on the live target: `13a-tool-propose-coupon`
  (id `pq7kv3LFX1sB9Eaf`) → `13b-tool-confirm-coupon` (id
  `xMMJvbeEhWgO638l`). First confirm: `created`, `coupon_id: 3156`,
  code `electronics-15-20260827`. Second confirm (same input): `duplicate`,
  same `coupon_id`. wp-admin confirms exactly one coupon in the store.