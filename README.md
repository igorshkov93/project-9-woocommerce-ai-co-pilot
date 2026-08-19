# WooCommerce AI Co-pilot

Telegram-based AI assistant for WooCommerce store owners. Answers analytical
questions about the store, drafts marketing copy, and performs administrative
actions through natural language — powered by LLM tool calling and orchestrated
in n8n.

> Status: work in progress.

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
| Notifications | Slack Incoming Webhook |

## Architecture

See [docs/](docs/).

## Demo

Coming soon.