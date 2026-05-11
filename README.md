# fatbot plugins

[Limnoria](https://github.com/progval/Limnoria) plugins running on a private IRC bot. Synced from the live server via `updatebot`.

## Plugins

### Claude
Lets users query Claude (Anthropic) in channel with `!claude <question>`. Maintains per-user conversation context (multi-turn). Filters out meta-questions (token usage, pricing, etc.). Auto-shortens URLs in responses via ShrinkUrl. Also ships an MCP server (`mcp_imageview.py`) for image-URL fetching used by the `claude` CLI on the host.

**Dependencies:** `anthropic` Python package, `curl_cffi`  
**Config:** `plugins.Claude.apiKey` (Anthropic API key), `plugins.Claude.channelEnabled` per-channel toggle

---

### Title
URL snarfer — automatically fetches and posts the `<title>` of any HTTP/S link posted in channel. Handles Twitter/X via the fxtwitter API, Reddit via old.reddit.com rewrite, and has anti-bot warm-up for Akamai/Cloudflare-protected sites. Skips binary URLs (images, video, audio). Optionally shortens via ShrinkUrl.

**Dependencies:** `curl_cffi`  
**Config:** `plugins.Title.enable` (per-channel), `plugins.Title.useShrinkUrl`, `plugins.Title.cookiesFile`

---

### ShrinkUrl
URL shortener snarfer and on-demand shortener. Supports multiple services: t.ly (default, via Bearer token), tinyurl, ur1.ca, x0.no, is.gd. Strips tracking parameters via `unalix` before shortening. Annotates shortened links with the source domain.

**Dependencies:** `unalix`, `requests`  
**Config:** `plugins.ShrinkUrl.default` (service name), `plugins.ShrinkUrl.tlyAccessToken` (t.ly Bearer token), `plugins.ShrinkUrl.shrinkSnarfer` (per-channel auto-shorten toggle)

---

### YouTube
Snarfer for YouTube video URLs. Fetches metadata (title, uploader, duration, view count, upload date, hashtags) via `yt-dlp` and posts a formatted info line with optional mIRC color prefix. Also shrinks the URL via ShrinkUrl. Handles regular videos, Shorts, and livestreams.

**Dependencies:** `yt-dlp` (binary at `/usr/bin/yt-dlp`)  
**Config:** `plugins.YouTube.snarfer`, `plugins.YouTube.cookiesFile` (Netscape cookies for bot-check bypass), `plugins.YouTube.shrink`

---

### NuWeather
Weather lookup with multiple backends: Pirate Weather, OpenWeatherMap, Weatherstack, WWIS. Geocodes locations via Nominatim, Google Maps, OpenCage, or Weatherstack. Caches geocode results locally.

**Dependencies:** `haversine`  
**Config:** Backend API keys via `plugins.NuWeather.*ApiKey`, `plugins.NuWeather.backend`

---

### ChanModes
Auto-enforces configured channel modes whenever the bot has ops. Reasserts modes on join and after any mode change.

**Config:** `plugins.ChanModes.modes` per channel (e.g. `+pnst`)

---

### Greeter
Sends a custom greeting message to specific nicks when they join `#fatkids`. Greetings are stored per-nick and persisted to disk.

**Commands:** `!addgreet <nick> <text>`, `!delgreet <nick>`, `!listgreets`

---

### Hamster
Posts "hamsters don't MAKE errors" to `#fatkids` at random intervals (5 minutes to 6 hours).

---

### InfoToggle
Owner-only admin shortcuts for common config tasks:

| Command | Effect |
|---|---|
| `!info [#chan] on\|off` | Toggle URL title fetching + shortening for a channel |
| `!ai [#chan] on\|off` | Enable/disable `!claude` for a channel |
| `!chanmode [#chan] <modes>` | Set auto-enforced channel modes |
| `!adduser <nick>` | Register a bot user from current hostmask |
| `!deluser <nick>` | Remove a bot user |
| `!cap <nick> <cap>` | Add a channel capability to a user |
| `!remcap <nick> <cap>` | Remove a channel capability from a user |

---

### Relay
Relays public chat from `#oldnews` into `#fatkids` in the format `<nick:#oldnews> message`.

---

### Repo
Replies with the GitHub repository URL for this bot (`!repo`).

---

## Global dependencies

```
pip install anthropic curl_cffi unalix requests haversine
```

`yt-dlp` must be installed as a system binary (`/usr/bin/yt-dlp`).

All API keys and tokens are configured via Limnoria's registry (`!config plugins.<Plugin>.<key> <value>`) — nothing is hardcoded.
