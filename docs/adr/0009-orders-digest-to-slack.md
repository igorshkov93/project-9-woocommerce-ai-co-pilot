# ADR 0009: Orders Digest to Slack

Status: Accepted

## Context

Phase 5, Step 17 of the roadmap. The store owner wants an on-demand summary
combining order stats for a period, the current top returned products, and
coupons created today, delivered to a Slack channel (`#orders-digest`, which
exists but has nothing connected to it yet) rather than inline in Telegram.

This builds directly on the conversational architecture established in
ADR 0008: `09-telegram-gateway` already routes free-text messages into
`15-ai-agent`, which reasons over a tool surface and replies conversationally.
Two of the three data points this digest needs already exist as standalone
tools (`11-tool-get-orders-summary`, `12-tool-get-top-returned-products`); the
third (coupons created today) does not.

## Decisions

### 1. New tool workflow: `16-tool-send-orders-digest`

Next available number after `15-ai-agent`. It aggregates all three data
points in one workflow rather than exposing "coupons created today" as its
own separate tool: `11-tool-get-orders-summary` and
`12-tool-get-top-returned-products` are called internally (Execute Workflow),
and coupons created today are fetched directly via
`GET /wp-json/wc/v3/coupons?after=<start of today>&before=<now>` (both
computed in `BUSINESS_TIMEZONE`, converted to the GMT/UTC ISO 8601 the
WooCommerce REST API expects for these parameters) - a single narrow query
that does not justify its own standalone tool.

### 2. Trigger: an AI Agent tool call, not a new gateway branch

The store owner asks for the digest in plain language (e.g. "пришли дайджест
по заказам в Slack"), which already reaches `15-ai-agent` through the
existing message branch. `16-tool-send-orders-digest` is added as a sixth
tool on the agent, with an LLM-facing description, the same way the other
five tools were added in ADR 0008. No new branch or command parser is added
to `09-telegram-gateway` - the existing conversational pattern is reused
rather than growing a second, parallel command surface.

### 3. Mutation safety: no propose/confirm split

Unlike coupon creation (ADR 0008, decision 4), sending a digest to Slack is
not treated as a dangerous mutation requiring a button-tap confirmation. It
moves no money, creates no persistent business record in the store, and the
worst case - an unwanted message in an internal Slack channel - is
low-stakes and manually correctable. The agent may call this tool directly
from a single natural-language request, consistent with how the other
read-only tools (`get_orders_summary`, `get_top_returned_products`) already
work without a confirmation step.

### 4. Period handling

`date_from`/`date_to` are supplied via `$fromAI`, mirroring how
`11-tool-get-orders-summary` already takes them directly - the LLM extracts
the period from however the store owner phrases the request. Per the
`$fromAI` default-value gotcha (project protocol), both fields carry a
default: the last 7 days, used when the store owner does not specify a
period. Coupons created today are never parameterized - "today" always means
the current day in `BUSINESS_TIMEZONE`, exactly as the requirement was
stated, independent of whatever period the orders summary covers.

### 5. Channel: environment variable, not hardcoded

The Slack node's target channel reads `$env.SLACK_DIGEST_CHANNEL` rather
than a channel name typed into the node. This matches the existing pattern
for `TELEGRAM_OWNER_CHAT_ID`: instance-specific configuration belongs in
`.env`, not baked into a sanitized workflow export that others might reuse.

### 6. Message format: Slack Block Kit

The digest is posted as Block Kit sections (with mrkdwn), not a single plain
text blob - it needs to present three distinct data groups (orders, returns,
coupons) legibly, and this is also the more presentable result for a
portfolio demo.

### 7. Response contract

`16-tool-send-orders-digest` returns `{status: 'sent' | 'error', message}`
to the agent. The agent confirms the outcome to the store owner in Telegram
(success, or what went wrong) - the digest's actual content is never
duplicated into the Telegram reply; Slack is the only place it appears.

## Prerequisite (not an architectural decision, but a build blocker)

No Slack app/bot token exists yet. `#orders-digest` is a real channel with
nothing connected to it. Before any node in `16-tool-send-orders-digest` can
be built and tested, a Slack app needs to be created with a `chat:write`
scope, installed to the workspace, invited into `#orders-digest`, and its
bot token added as a credential in n8n.

## Consequences

- The store owner gets the digest through Slack, matching where the rest of
  the team presumably already looks for operational updates, without
  cluttering the Telegram conversation with a long formatted message.
- `15-ai-agent`'s tool surface grows to six tools; the system prompt (ADR
  0008, decision 7) may need a short addition so the agent knows when a
  request means "send to Slack" versus "just tell me here."
- `16-tool-send-orders-digest` is the first tool with an external side
  effect that isn't gated by a confirm step (decision 3) - if a future tool
  has a side effect that is *not* obviously low-stakes, decision 3's
  reasoning should not be assumed to apply by default; it needs its own
  explicit safety judgment, the way ADR 0008 decision 4 made one for
  coupons.
- Adding `SLACK_DIGEST_CHANNEL` to `.env` means `.env.example` needs the new
  key too, and the Slack credential needs to exist in n8n before this
  workflow's nodes can be tested at all (see Prerequisite above).