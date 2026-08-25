# ADR 0003: Vector search for catalog entity resolution

Date: 2026-08-24
Status: Accepted

## Context

The copilot receives free-form Russian text from Telegram ("создай купон на
Электронику", "сколько продали наушников"), while the WooCommerce catalog is in
English: `Electronics`, `ELEC-EARBUD-01`, `Wireless Earbuds Pro`. Every tool
that touches the store needs a numeric ID, so something has to bridge the gap
between what the owner typed and what the API expects.

Exact string matching does not survive this gap. "наушники" and "Wireless
Earbuds Pro" share no tokens at all. Beyond language, the catalog carries
HTML entities in category names (`Bras &amp; Tanks`) and the owner will phrase
the same thing differently on different days.

A second problem is confidence. An exact match returns a boolean: found or not.
Coupon creation is irreversible, so the tool must be able to say "I am not sure
which one you mean" instead of silently picking a candidate.

## Decision

Index the catalog into Qdrant and resolve entities by semantic similarity.

**Storage.** Single collection `copilot_catalog`, 768-dimensional vectors,
Cosine distance. Payload indexes on `entity_type`, `entity_id`, `product_type`.

**Embeddings.** Gemini `models/gemini-embedding-001` truncated to 768 dimensions
via Matryoshka. Documents are embedded with `RETRIEVAL_DOCUMENT`, queries with
`RETRIEVAL_QUERY`.

**Point IDs.** UUID5 over `"{entity_type}:{entity_id}"`. Re-indexing overwrites
rather than duplicating; Qdrant accepts only UUIDs or unsigned integers, so the
natural key is hashed rather than used directly.

**Indexed scope.** All 24 products plus the 7 non-empty categories. The other 37
categories are empty placeholders; offering them to the agent would let it
create coupons for categories nobody can buy from.

**Embedded text.** Name, categories, SKU and description joined together — not
the bare name, which is too sparse to distinguish anything. WooCommerce sample
placeholders ("This is a simple product.") are stripped: they are identical
across half the catalog, and an identical trailing sentence pulls unrelated
products closer together under cosine similarity.

**Contract.** The tool returns one item with a three-valued `status`:

| status | condition | agent behaviour |
|---|---|---|
| `resolved` | top score >= 0.60 and no competing runner-up | act on `match.entity_id` |
| `ambiguous` | top score >= 0.60, runner-up also >= 0.60 within 0.05 | ask the user which one |
| `not_found` | top score < 0.60 | report honestly, do not guess |

## Calibration

Thresholds are measured, not chosen. From `verify_catalog_index.py`:

| class | top-1 score |
|---|---|
| correct hits (8 cases, RU and EN) | 0.645 – 0.739 |
| nonsense queries ("как испечь хлеб", "car insurance") | 0.545 – 0.550 |

The gap is 0.095 with no overlap. **0.60** sits in its middle with equal margin
on each side; at 31 points the sample is too small to justify hugging either
edge.

Cross-language queries score consistently lower than their English equivalents
("Электроника" 0.661 vs "electronics" 0.692), so the floor is calibrated
against the Russian case, which is the one users will actually type.

The **0.05** ambiguity margin exists because a filtered search always returns
something. It applies only when the runner-up clears 0.60 on its own. Without
that guard, "наушники" would be reported as ambiguous because a music track
named `Single` scored 0.599 — barely above noise. With it, "толстовки" is still
ambiguous, and correctly so: `Clothing` (0.649) is the genuine parent category
of `Hoodies` (0.685).

Observed behaviour across seven scenarios: five resolve cleanly, one asks, one
is refused. The demo case "купон на Электронику" resolves to `ambiguous`
(margin 0.042) and will prompt for confirmation. This is accepted rather than
tuned away — see below.

## Consequences

Positive:

- Russian queries resolve against an English catalog with no translation layer
- The agent can distinguish "not found" from "found something weak"
- Deterministic IDs make re-indexing idempotent
- The tool layer is provider-agnostic: swapping the embedding model requires
  re-indexing but no change to the contract

Negative:

- The index is a snapshot. Catalog changes require re-running fetch + index
- One more service in the compose stack
- Embedding calls consume a limited free-tier quota

## Alternatives considered

**Exact string matching against product and category names.** Rejected: fails
on cross-language queries entirely, and returns a boolean where a confidence
signal is needed. Name collisions in the raw catalog (three `Tees`, three
`Pants`) would compound this, though filtering to non-empty categories happens
to remove all collisions among indexed entities — that filtering is what solves
the collision problem, not the vectors.

**OpenAI embeddings.** No API key available for this project. Recorded as a
constraint rather than a preference: the interface would be identical.

**Lowering the ambiguity margin to 0.03.** This would make the demo case resolve
directly. Rejected as fitting the threshold to a single desired outcome; 0.05
is derived from the observed score distribution, 0.03 is derived from wanting a
particular answer. Asking for confirmation before an irreversible action is
defensible behaviour, not a defect.

## Known limitations

- `Clothing` reports `product_count = 14`, counting descendants. A WooCommerce
  coupon restricted to a parent category does **not** apply to its children —
  step 13 must account for this
- 31 points is a small sample for threshold calibration; the numbers above hold
  for this catalog and would need re-measuring on a larger one
- `sanitize_workflow.py` warns when no credential ID is replaced, which fires
  falsely on workflows that legitimately have none (step 18)