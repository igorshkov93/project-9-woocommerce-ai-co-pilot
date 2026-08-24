# ADR 0002: Telegram access control and env var access in Code nodes

- Status: accepted
- Date: 2026-08-24
- Supersedes: none

## Context

The bot is exposed to the public internet through a static ngrok domain, and
any Telegram user who finds `@proj9wooshopbot` can send it messages. Later
steps add tools with side effects (`create_coupon`, step 13) and LLM calls
that consume a limited free-tier quota. An unauthenticated caller could
therefore mutate store data and exhaust the daily model budget.

Telegram itself provides no authorization: the bot receives every update sent
to it. Authorization must be implemented in the workflow.

## Decision

**1. A fail-closed whitelist is the first node after the trigger.**

`guard-whitelist` (Code node) classifies each update, and `route-access` (If
node) routes it. Only chat IDs listed in `TELEGRAM_OWNER_CHAT_ID` reach any
downstream node. Everything else is routed to a no-op branch.

Matching is on `chat.id`, not `username`: usernames are mutable and optional.

Denied updates receive no reply. Returning "access denied" would confirm the
bot is live and give an unauthenticated caller a way to trigger executions.

**2. The whitelist lives in an environment variable, not in the node.**

The workflow JSON is committed to a public repository. A chat ID hardcoded in
node parameters would be published with it. `$env` keeps the value in `.env`,
which is gitignored.

**3. `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` is set for this instance.**

n8n blocks `$env` in Code nodes by default — a sensible guard in multi-tenant
deployments, where workflow authors should not read host secrets. This is a
single-user local stand where the author and the operator are the same person,
so the guard protects nothing while forcing the alternative (hardcoding the
chat ID in the node) that decision 2 exists to prevent.

## Consequences

**Positive**

- One enforcement point instead of a check duplicated in every tool.
- Access decisions are visible in execution history: the guard emits
  `allowed` and `reason` on every update, and the denied branch is a node on
  the canvas rather than an empty result.
- The published workflow contains no personal identifiers.

**Negative**

- `$env` access is available to any Code node in this instance. Acceptable
  locally; a hosted deployment should instead inject the whitelist through a
  credential or an external config store.
- Adding an authorized user requires editing `.env` and recreating the
  container. Acceptable for a single-owner bot.

**Neutral**

- Denied callers see silence, which is indistinguishable from an outage. The
  audit trail in execution history is the operator-side compensation.

## Alternatives considered

- **Checking the chat ID inside each tool sub-workflow.** Rejected: the check
  would be duplicated 5+ times, and a single omission opens the whole surface.
- **A shared secret phrase in the message body.** Rejected: it would appear in
  plaintext in every chat and in Telegram's servers, and adds a step to every
  interaction without binding to an identity.
- **Hardcoding the chat ID in node parameters.** Rejected: leaks a personal
  identifier into a public repository.

## Verification

Both paths are covered by manual tests recorded in step 9:

- Allowed chat: update routed to `true`, echo delivered.
- Foreign chat (pinned fixture, `chat.id = 111111111`): routed to `false`,
  `echo-reply` not executed, no message sent.