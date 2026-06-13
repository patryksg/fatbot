# SportsNews

Fetches the latest sports headlines from ESPN's public API and posts the top
three as one IRC line each. Article URLs are shortened through the bot's
`ShrinkUrl` plugin (t.ly chain) and colored blue, matching the rest of the
bot's links.

## Commands

| Command | Description |
|---|---|
| `!sports <league>` | Top 3 ESPN headlines for the league, each as `headline - short-url` |

An unknown league replies with the full list of supported leagues.

## Leagues

`nfl`, `nba`, `wnba`, `mlb`, `nhl`, `mls`, `ncaaf`, `ncaab`, `epl`, `laliga`,
`seriea`, `bundesliga`, `ligue1`, `ucl`, `ufc`, `f1`, `pga`

## Requirements

- Network access to `site.api.espn.com` (no API key required)
- The `ShrinkUrl` plugin loaded for t.ly link shortening — falls back to the
  full URL (still colored blue) if it is unavailable
