# Wikibear

Wiki Bear, the deadpan talking-teddy bit from Conan O'Brien. Recites a real Wikipedia fact and pivots from harmless trivia into something weird, creepy, gruesome, or nihilistic — flat cheery toy-robot cadence, no warnings, no winks.

## Commands

| Command | Description |
|---|---|
| `!wikibear` | Random creepy/cheerful Wikipedia factoid, with source URL |
| `!wikibear <question>` | Answers the question from Wikipedia, then segues `Speaking of X...` into a tangential horror fact, both sourced |

Output is capped to 1–3 IRC messages (≤380 chars each) and always ends with `I'm Wiki Bear.` on its own line.

## Requirements

- `claude` CLI with Pro OAuth (WebSearch tool)
- Cloudflare WARP SOCKS5 proxy on `127.0.0.1:40000` for `is.gd` URL shortening
- Channels must have the `wikibear` capability enabled
