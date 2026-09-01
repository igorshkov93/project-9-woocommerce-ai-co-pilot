🌐 **English** | [Українська](README.uk.md) | [Русский](README.ru.md)

# WooCommerce AI Co-pilot

A Telegram-based AI assistant for WooCommerce store owners. It answers
analytical questions about the store, drafts marketing copy, and performs
administrative actions through natural language — powered by LLM tool calling
and orchestrated in n8n.

> Status: core flows implemented and working end-to-end; final QA pass on two
> recent fixes in progress.

## Why this exists

Running a small WooCommerce store means constantly switching between wp-admin,
spreadsheets, and marketing tools just to answer routine questions or take
routine actions. This assistant replaces that switching with a Telegram
conversation:

- **"How did we do this week?"** — instead of building a report by hand, the
  owner asks in plain language and gets a summary on the spot, or receives it
  automatically every day as a Slack digest.
- **A win-back email for an abandoned cart** — instead of writing copy from
  scratch, the assistant drafts it in the store's voice, on demand.
- **A targeted coupon for a slow-moving category** — instead of navigating
  WooCommerce's coupon UI and manually resolving the category, the owner
  describes what they want and confirms a proposal before anything is
  created. Nothing is written to the store without an explicit human
  confirmation step.

The goal: turn day-to-day store admin into a five-second Telegram message,
without giving an LLM unsupervised write access to the store.

## Capabilities

- Daily order and revenue summaries
- Top returned products analysis
- Abandoned cart email campaign drafting
- Coupon creation with human-in-the-loop confirmation
- Scheduled daily digest to Slack

## Stack

| Layer | Technology |
|---|---|
| Interface | Telegram Bot API |
| Orchestration | n8n (self-hosted, Docker) |
| Reasoning | Gemini Flash (routing, tool calling), Claude Haiku (generation) |
| Vector store | Qdrant |
| Data source | WooCommerce REST API |
| Notifications | Slack (direct API call — see [ADR 0009](docs/adr/0009-orders-digest-to-slack.md)) |

## Architecture

A single Telegram gateway workflow routes every incoming message or button
tap to an AI Agent workflow, which reasons over six tools (store lookups,
analytics, coupon proposal, email drafting, Slack digest) backed by their own
sub-workflows. Actions that change store state (coupon creation) go through
an explicit propose → confirm flow: the agent can only *propose*, and nothing
is created until the owner taps a confirmation button, which triggers a
separate, narrowly-scoped workflow.

Design decisions and trade-offs are recorded as ADRs in [docs/adr/](docs/adr/):

- [0008 — AI agent orchestration](docs/adr/0008-ai-agent-orchestration.md)
- [0009 — Orders digest to Slack](docs/adr/0009-orders-digest-to-slack.md)

## Demo

Coming soon (Loom walkthrough).