# ADR 0008: AI Agent Orchestration (Gemini Flash Tool-Calling Agent)

Status: Accepted

## Context

Phases 1-3 built seven standalone n8n sub-workflows, each callable directly
with a fixed input shape: `resolve_entity`, `get_orders_summary`,
`get_top_returned_products`, `13a-tool-propose-coupon`
(`pq7kv3LFX1sB9Eaf`), `13b-tool-confirm-coupon` (`xMMJvbeEhWgO638l`), and
`14-tool-draft-abandoned-cart-email` (`qcjOmx2r0K6bTmP2`). None of them are
wired to a real conversational entry point yet - the Telegram gateway
(`09-telegram-gateway`) only ever called them directly with hand-built test
input.

Step 15 replaces that with a real conversational layer: a store owner
messages the Telegram bot in natural language, an LLM decides which of the
above tools (if any) to call, and the bot replies in natural language
grounded in the tool output. This ADR fixes the architecture before any
n8n nodes are built.

## Decisions

### 1. Agent framework: n8n's native AI Agent node

Use n8n's built-in "AI Agent" node (LangChain-based tool-calling agent)
rather than a hand-rolled tool-calling loop in Code nodes. It is n8n's
standard idiom for this pattern, handles the reasoning/tool-call/
tool-result loop internally, and is what an n8n-based AI Automation
portfolio piece is expected to demonstrate.

### 2. Reasoning/tool-calling model: Google Gemini Flash

The agent's Chat Model input is Google Gemini Flash (free tier), consistent
with the project's existing cost-conscious model routing: Gemini handles
orchestration and tool selection at zero marginal cost, keeping the ~$8.72
Anthropic budget reserved for where a paid model earns its cost.

### 3. Tool surface exposed to the agent

Each of the following existing sub-workflows is wrapped in its own
"Call n8n Workflow Tool" node and attached to the AI Agent:
`resolve_entity`, `get_orders_summary`, `get_top_returned_products`,
`13a-tool-propose-coupon`, `draft_abandoned_cart_email`. Each tool's
description (the text the LLM sees when deciding whether to call it) is
written specifically for LLM tool-selection, separately from any
human-facing documentation, and states the tool's contract statuses so the
agent knows what shapes to expect back.

### 4. Mutation safety: `13b-tool-confirm-coupon` is NOT an agent tool

`13b-tool-confirm-coupon` is deliberately excluded from the agent's tool
list. The agent may only call `13a-tool-propose-coupon` (which creates
nothing - it returns a proposal plus enough data to build the Telegram
inline-button payload, see decision 9). Actually creating the coupon may
only happen when a real button tap produces a `callback_query` update,
handled by decision 5 below. This prevents a scenario where the LLM
misreads a user's message as confirmation ("yeah sounds good", sarcasm, a
follow-up question that isn't actually a yes) and mutates the store on a
hallucinated confirmation. The button tap is the only valid confirmation
signal, by construction.

### 5. Gateway routing: text vs callback_query

`09-telegram-gateway` is extended with a branch on Telegram update type,
upstream of the AI Agent:
- A normal text message is routed into the AI Agent node.
- A `callback_query` (an inline-button tap) is routed directly to
  `13b-tool-confirm-coupon`, bypassing the AI Agent entirely - the agent
  never sees or reasons about confirmations, per decision 4.

### 6. Conversation memory: per-chat window buffer

The AI Agent is given a Window Buffer Memory node keyed by the Telegram
`chat_id`, so a multi-turn conversation (e.g. "show me return numbers for
Electronics" -> "now make a coupon for that category") retains context
without the user having to restate it every message.

### 7. System prompt requirements

The agent's system prompt establishes: its role (an operational assistant
for the store owner, not a customer-facing agent); a summary of each
available tool and when to call it; a hard rule against fabricating any
number, product name, or claim not present in a tool's output (the same
anti-hallucination discipline established for `draft_abandoned_cart_email`
in ADR 0007); and an instruction to ask a clarifying question rather than
guess when a request is ambiguous (e.g. which category, which customer).

### 8. Final-reply model: Gemini Flash, not Claude Haiku

The AI Agent's own model (Gemini Flash) composes the final natural-language
reply sent back to the store owner, using the tool results already in its
context - there is no separate hand-off to Claude Haiku for general replies.
Claude Haiku remains reserved exclusively for `draft_abandoned_cart_email`,
the one place actual customer-facing marketing prose is generated; the
store owner's own operational summaries (order stats, return counts,
coupon confirmations) don't need paid-model polish, and routing every
agent turn through a second paid model would eat into the project's
Anthropic budget for no meaningful quality gain here.

### 9. Coupon-proposal button delivery: post-agent inspection, not agent-generated

An n8n AI Agent node replies in plain text only - it cannot itself emit a
Telegram inline keyboard. `13a-tool-propose-coupon`'s own JSON contract
(status/category/would_be_duplicate/payload) also carries no `callback_data`
or Telegram-specific fields, and is left that way deliberately: it stays a
pure, Telegram-agnostic, independently testable API tool.

Instead:
- **`callback_data` format**: a compact delimited string,
  `cc:{category_id}:{discount_percent}:{expires_days}` (e.g. `cc:67:15:14`),
  well within Telegram's 64-byte limit and trivially parsed with `split(':')`.
- **Who builds the button**: the AI Agent node runs with "Return
  Intermediate Steps" enabled. A Code node placed immediately after the
  agent inspects `intermediateSteps` for a call to the `propose-coupon`
  tool whose result status is `proposed` (not `duplicate`), and if found,
  builds `{label, callback_data}` from that call's original input
  parameters.
- **Who sends to Telegram**: neither the agent nor the code node above -
  the AI Agent sub-workflow returns `{reply_text, button: {label,
  callback_data} | null}` to its caller, and `09-telegram-gateway` (which
  already owns the Telegram credential and chat_id) sends the actual
  message(s): the agent's `reply_text`, plus a follow-up message with the
  inline button when `button` is non-null. This keeps all Telegram-specific
  concerns (credentials, chat_id, message formatting) centralized in the
  gateway, matching the existing pattern of `13b` rebuilding auth locally
  rather than trusting upstream-supplied credentials.

## Consequences

- The store owner gets a real conversational interface instead of manual
  test-input calls to each sub-workflow.
- Coupon creation cannot be triggered by conversation alone, only by an
  explicit button tap - the two-phase propose/confirm design from Step 13
  is now load-bearing for safety, not just idempotency.
- The AI Agent sub-workflow's output contract is `{reply_text, button}`,
  not a bare string - `09-telegram-gateway` must be built to handle this
  shape, sending one or two Telegram messages depending on whether `button`
  is present.
- Adding a new tool later means: build its sub-workflow, wrap it in a
  Call n8n Workflow Tool node, write an LLM-facing description, and decide
  whether it needs the same propose/confirm split as coupons (i.e. any
  future store-mutating tool follows the same pattern as decision 4), and
  whether it needs the same post-agent button-building treatment as
  decision 9.
- If Gemini Flash's free-tier reply quality proves insufficient for
  interview-facing demos, decision 8 can be revisited without touching
  decisions 1-7 and 9.

## Addendum: Implementation status (31.08.2026)

All 9 decisions above were implemented and confirmed end-to-end against a
live Telegram bot and a live WooCommerce store, not just individually
tested sub-workflows. Both entry-point branches of `09-telegram-gateway`
are built and published: a plain-text message routes into `15-ai-agent`
(decision 5), and a real button tap on a proposed coupon runs the full
propose -> button -> `callback_query` -> `13b-tool-confirm-coupon` ->
real `POST /coupons` circuit, producing an actual coupon in the store.

Two implementation details emerged during the build that are worth
recording alongside the decisions above:

- **Decision 6 (memory), concrete node**: n8n's window-buffer memory is
  the **Simple Memory** node. Configured with Session ID bound to the
  Telegram `chat_id` and Context Window Length = 5.
- **Decision 9 (button delivery), a non-obvious gotcha**: when the AI
  Agent's `intermediateSteps` are inspected for a `propose_coupon` tool
  call, the recorded `observation` is a JSON-stringified **array**
  (`"[{...}]"`), not a flat object. `build-coupon-button` must
  `JSON.parse` it and read the first array element before accessing
  `status`/`category`/etc. - reading the parsed value directly silently
  fails.

One gap outside the original 9 decisions was found and closed in
follow-up hardening, not by this ADR: decisions 4 and 5 establish that
`13b-tool-confirm-coupon` is the only path that can mutate the store, but
neither decision addresses what the gateway does when `13b` itself
reports something other than success. In practice `13a-tool-propose-coupon`
(called internally by `13b`) can also return `invalid_input` or `no_data`
(malformed input, or a category id that does not exist) - two failure
statuses this ADR never scoped. Originally the gateway conflated both with
a genuine duplicate-coupon response, showing the user a "coupon already
exists" message with no `code`/`coupon_id` to fill in. `09-telegram-gateway`
now has an additional `route-coupon-duplicate` check ahead of a dedicated
`coupon-error-reply` node, so a real duplicate, an invalid request, and an
unresolvable category id each get their own accurate reply to the store
owner. This does not change decisions 1-9; it closes a response-handling
gap that sat outside their scope.