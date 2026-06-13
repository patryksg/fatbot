# Claude

IRC interface to the Claude Code CLI. Provides AI responses in-channel with
context memory, channel-brain injection, and an MCP tool suite (image view,
URL fetch, YouTube transcript/download, Reddit video analysis).

Questions are asked by **addressing the bot by nick** (`botnick: <question>`).
The commands below only switch the channel's model mode.

## Commands

| Command | Description |
|---|---|
| `!claude` / `!haiku` | Switch channel to Claude Haiku mode (default, cheap) |
| `!fable` | Switch channel to Claude Fable mode (highest model, `--effort max` — expensive) |

Mode switching is owner-only and requires the channel `ai` capability.
Up to 8 reply lines per answer in both modes.

- **Context memory**: per-user conversation context is retained for up to 5 turns / 6 minutes, then cleared.
- **Channel brain**: a strictly per-channel digest (`<slug>info.md`, e.g. `#yourchannel` → `yourchannelinfo.md`) is injected into the system prompt so the bot has context about that channel's conversations. Gated by the channel `brain` capability; a channel only ever loads its own file, never another channel's.

## Models

Configurable live from IRC, no reload needed:

```
config plugins.Claude.haikuModel  <model>     # default claude-haiku-4-5-20251001
config plugins.Claude.fableModel  <model>     # default claude-fable-5
config plugins.Claude.fableEffort <low|medium|high|xhigh|max>
```

Haiku mode never sends `--effort` (Haiku 4.5 doesn't support it).

> **Note (2026-06-13):** the old `!smart`/`!gem` modes and the Gemini chat
> fallback were removed. Gemini is no longer part of the chat path; it is
> only used by the MCP video tools (`mcp_youtube.py`, `mcp_reddit.py`) for
> video understanding, which the Claude CLI cannot do.

## Requirements

- `claude` CLI in `$PATH` (configured at `/home/botuser/.local/bin/claude`)
- `GEMINI_API_KEY` environment variable (passed through to the MCP video tools)
- MCP image-view config at `plugins/Claude/mcp-imageview.json`

## Capabilities

The `claude` capability must be granted to a user to ask questions:

```
!admin capability add <nick> claude
```
