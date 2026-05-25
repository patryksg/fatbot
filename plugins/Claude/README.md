# Claude

IRC interface to the Claude API (and Gemini as a fallback). Provides AI responses in-channel with context memory, channel-brain injection, and MCP image-view support.

## Commands

| Command | Description |
|---|---|
| `!claude <question>` | Single-line answer via Claude Haiku |
| `!smart <question>` | Up to 3-line answer via Claude Haiku |
| `!gem <question>` | Force Gemini 2.5 Flash (owner-only) |

- **Context memory**: per-user conversation context is retained for up to 5 turns / 6 minutes, then cleared.
- **Channel brain**: a strictly per-channel digest (`<slug>info.md`, e.g. `#yourchannel` → `fatkidsinfo.md`) is injected into the system prompt so the bot has context about that channel's conversations. Gated by the channel `brain` capability; a channel only ever loads its own file, never another channel's.
- **Gemini fallback**: if Claude returns a rate-limit error, the bot automatically retries via Gemini 2.5 Flash and appends `(gem)` to the reply.

## Requirements

- `claude` CLI in `$PATH` (configured at `/home/botuser/.local/bin/claude`)
- `GEMINI_API_KEY` environment variable for fallback
- MCP image-view config at `plugins/Claude/mcp-imageview.json`

## Capabilities

The `claude` capability must be granted to a user to use these commands:

```
!admin capability add <nick> claude
```
