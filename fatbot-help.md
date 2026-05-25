# fatbot — Command & Settings Inventory

Auto-generated from plugin sources. Do not edit by hand.

## Aliases

Owner shortcuts for common admin tasks.

!addhost <user> <hostmask>   add a hostmask to a registered user

**Commands:**
- `!addhost` — <user> <hostmask>

## Ash

!ashnormal/!ashsmart/!ashgem — Ash Williams from Evil Dead, in character.

**Commands:**
- `!ashnormal` — [<question>] — Ash answers via Claude haiku (cheap).
- `!ashsmart` — [<question>] — Ash answers via Claude opus (smart).
- `!ashgem` — [<question>] — Ash answers via Gemini Flash (cheap).

## ChanModes

Auto-enforces configured channel modes when the bot has ops.

Set per channel:  !config channel #foo plugins.ChanModes.modes +pnst
Disable:          !config channel #foo plugins.ChanModes.modes ""

**Config:**
- `supybot.plugins.ChanModes.modes` (channel, String) — default `''` — Modes to enforce on this channel when the bot has ops. Format like '+pnst' or '+pnst-ik'. Empty disables enforcement.

## Claude

Per-channel multi-model Q&A. Switch model with !claude / !smart / !gem
(no args). Ask by addressing the bot by nick — e.g. 'fatbot, what is love?'.
Auto-switches to gem on Claude rate-limit.

**Commands:**
- `!claude` — takes no arguments
- `!smart` — takes no arguments
- `!gem` — takes no arguments

**Config:**
- `supybot.plugins.Claude.channelEnabled` (channel, Boolean) — default `False` — Whether !claude is enabled in this channel.
- `supybot.plugins.Claude.smartMode` (channel, Boolean) — default `False` — DEPRECATED. Replaced by `mode`. Kept only for backward compat.
- `supybot.plugins.Claude.mode` (channel, ClaudeMode) — default `'haiku'` — Active model for this channel. "haiku" = Claude Haiku single-line. "opus" = Claude Opus up to 3 lines (smart). "gem" = Gemini 2.5 Flash single-line. Switched in-channel via !claude / !smart / !gem.
- `supybot.plugins.Claude.geminiFallback` (channel, Boolean) — default `True` — When True and current mode is haiku/opus, on a Claude rate-limit / quota error the channel auto-switches to "gem" mode and the question is answered by Gemini. The auto-fallback answer is suffixed with "(gem)".

## Create

Image/video generation: !pic, !picnsfw, !video, !videonsfw.

**Commands:**
- `!pic` — <prompt> | <image-url> <edit> — generate an SFW image via Gemini
- `!picnsfw` — <prompt> | <image-url> <edit> — generate an image via Flux (NSFW-capable),
- `!video` — <prompt> — Flux Pro image → Atlas Wan 2.2 Turbo I2V.
- `!videonsfw` — <prompt> — Flux Pro image → Atlas Cloud Wan 2.2 Turbo Spicy I2V.
- `!cap` — <nick> [capname] — grant capability (default: generative) in this channel.
- `!uncap` — <nick> [capname] — revoke capability (default: generative) in this channel.
- `!chancap` — [<channel>] <capname> — enable feature <capname> in <channel> (defaults to current).
- `!unchancap` — [<channel>] <capname> — disable feature <capname> in <channel> (defaults to current).

**Config:**
- `supybot.plugins.Create.model` (channel, String) — default `'bfl:3@1'` — Runware model id for !picnsfw (default: Flux Pro, NSFW-capable).
- `supybot.plugins.Create.timeoutSec` (global, PositiveInteger) — default `120` — HTTP timeout for the Runware image generation request.
- `supybot.plugins.Create.videoTimeoutSec` (global, PositiveInteger) — default `900` — Total time to wait for video generation (Atlas/fal) to finish.
- `supybot.plugins.Create.picModel` (channel, String) — default `'bfl:3@1'` — Runware model id for !pic and seed image of !video (default: Flux Pro, high quality).
- `supybot.plugins.Create.editModel` (channel, String) — default `'bfl:3@1'` — Runware model id for instruction edits of !picnsfw <url> <edit> (default: FLUX.1 Kontext Pro). SFW-only; NSFW edits fall back to editFallbackModel.
- `supybot.plugins.Create.editFallbackModel` (channel, String) — default `'civitai:1195276@1345786'` — Uncensored Runware model (Lustify SDXL) for the img2img fallback when Kontext refuses an NSFW edit on !picnsfw <url> <edit>.
- `supybot.plugins.Create.editStrength` (channel, String) — default `'0.6'` — img2img strength (0-1) for the uncensored NSFW edit fallback; higher = further from the source image.

## Greeter

Greets registered users when they join #yourchannel.

**Commands:**
- `!addgreet` — <nick> <greeting> -- Adds a greeting for <nick> when they join #yourchannel.
- `!delgreet` — <nick> -- Removes the greeting for <nick>.
- `!listgreets` — Lists all stored greetings.

## Hamster

Randomly says hamsters don't MAKE errors in #yourchannel.

## InfoToggle

Admin shortcuts: !info, !ai, !chanmode, !chancap, !unchancap, !adduser, !deluser, !cap, !remcap.

**Commands:**
- `!info` — [<#channel>] on|off
- `!ai` — [<#channel>] on|off
- `!chanmode` — [<#channel>] <modes>
- `!adduser` — <nick>
- `!deluser` — <nick|username>
- `!cap` — <nick|username> <capability>
- `!remcap` — <nick|username> <capability>
- `!chancap` — [<#channel>] <capability>
- `!unchancap` — [<#channel>] <capability>

## NuWeather

Weather plugin for Limnoria

**Commands:**
- `!weather` — [--user <othernick>] [--weather-backend/--backend <weather backend>] [--geocode-backend <geocode backend>] [--forecast] [<location>]
- `!geolookup` — [--backend <backend>] <location>
- `!setweather` — <location>
- `!aqi` — [--geocode-backend <backend>] <location>

**Config:**
- `supybot.plugins.NuWeather.?` (global)
- `supybot.plugins.NuWeather.temperature` (channel, NuWeatherTemperatureDisplayMode) — default `'F/C'`
- `supybot.plugins.NuWeather.distance` (channel, NuWeatherDistanceDisplayMode) — default `'$mi / $km'`
- `supybot.plugins.NuWeather.speed` (channel, NuWeatherDistanceDisplayMode) — default `'$mi / $km'`
- `supybot.plugins.NuWeather.defaultBackend` (channel, NuWeatherBackend) — default `BACKENDS[0]`
- `supybot.plugins.NuWeather.geocodeBackend` (channel, NuWeatherGeocode) — default `GEOCODE_BACKENDS[0]`
- `supybot.plugins.NuWeather.aqicn` (global, String) — default `''`
- `supybot.plugins.NuWeather.stripColors` (channel, Boolean) — default `False`
- `supybot.plugins.NuWeather.stripFormatting` (channel, Boolean) — default `False`
- `supybot.plugins.NuWeather.outputFormat` (channel, String) — default `''`
- `supybot.plugins.NuWeather.currentOnly` (channel, String) — default `''`
- `supybot.plugins.NuWeather.forecast` (channel, String) — default `''`
- `supybot.plugins.NuWeather.?` (global, String) — default `''`

## Relay

Relays public chat from #yourchannel2 to #yourchannel.

## Repo

Replies with the GitHub repo URL or install guide for this bot.

**Commands:**
- `!repo` — takes no arguments
- `!howto` — takes no arguments

## ShrinkUrl

This plugin features commands to shorten URLs through different services,
like tinyurl.

**Commands:**
- `!shrinkSnarfer`
- `!tiny` — <url>
- `!ur1` — <url>
- `!x0` — <url>
- `!tly` — <url>

**Config:**
- `supybot.plugins.ShrinkUrl.shrinkSnarfer` (channel, Boolean) — default `False`
- `supybot.plugins.ShrinkUrl.showDomain` (channel, Boolean) — default `True`
- `supybot.plugins.ShrinkUrl.minimumLength` (channel, PositiveInteger) — default `48`
- `supybot.plugins.ShrinkUrl.nonSnarfingRegexp` (channel, Regexp) — default `None`
- `supybot.plugins.ShrinkUrl.outFilter` (channel, Boolean) — default `False`
- `supybot.plugins.ShrinkUrl.default` (channel, ShrinkService) — default `'tly'`
- `supybot.plugins.ShrinkUrl.bold` (global, Boolean) — default `True`
- `supybot.plugins.ShrinkUrl.tlyAccessToken` (global, String) — default `''`
- `supybot.plugins.ShrinkUrl.serviceRotation` (channel, ShrinkCycle) — default `[]`

## Title

Posts the HTML <title> of URLs mentioned in channel.

Per-channel toggle:  !config channel #foo plugins.Title.enable True
Skip pattern:        !config channel #foo plugins.Title.nonSnarfingRegexp m/youtube/i

**Commands:**
- `!title` — <url>

**Config:**
- `supybot.plugins.Title.enable` (channel, Boolean) — default `False` — If enabled, fatbot will fetch the HTML <title> of any URL posted in this channel and post it back.
- `supybot.plugins.Title.nonSnarfingRegexp` (channel, Regexp) — default `None` — URLs matching this regexp will not be snarfed in this channel. Empty disables the filter.
- `supybot.plugins.Title.format` (channel, String) — default `':: %s'` — Format string for the snarfed title. The literal %s is replaced by the page title.
- `supybot.plugins.Title.timeout` (global, Float) — default `6.0` — HTTP request timeout in seconds (per attempt; redirects each get a fresh timeout).
- `supybot.plugins.Title.maxBytes` (global, PositiveInteger) — default `262144` — Maximum bytes to read from each URL while looking for the <title> tag.
- `supybot.plugins.Title.maxLength` (global, PositiveInteger) — default `380` — Maximum byte length of the IRC reply line (excluding the IRC envelope). Lines longer than this are truncated with an ellipsis.
- `supybot.plugins.Title.cookiesFile` (global, String) — default `''` — Optional absolute path to a Netscape-format cookies.txt. When set, cookies from that file are merged into the shared HTTP session, scoped per domain by the cookie jar. Useful for sites that block unauthenticated requests (e.g. reddit.com). The file is reloaded automatically when its mtime changes.
- `supybot.plugins.Title.userAgent` (global, String) — default `'Mozilla/5.0 (compatible; fatbot-title/1)'` — User-Agent header sent when fetching URLs (ignored when curl_cffi is installed -- impersonation dictates the headers).
- `supybot.plugins.Title.useShrinkUrl` (channel, Boolean) — default `False` — If True, ask the ShrinkUrl plugin to shorten the URL using its configured service and post output as '<short> | <title>'. You should also disable `supybot.plugins.ShrinkUrl.shrinkSnarfer` for this channel to avoid duplicate output.

## TopicLock

Snapshot a channel's topic and revert any change until unlocked.

!topic lock [<channel>]    snapshot the current topic and guard it
!topic unlock [<channel>]  release the guard
!topic status [<channel>]  show whether a channel is locked

**Commands:**
- `!topic` — <lock|unlock|status> [<channel>]

## Wikibear

!wikibear — wiki bear shares creepy/absurd Wikipedia factoids.

**Commands:**
- `!wikibear` — [<question>] — wiki bear shares an absurd Wikipedia factoid, or

**Config:**
- `supybot.plugins.Wikibear.enabled` (channel, Boolean) — default `False` — !wikibear is only available in channels where this is True.
- `supybot.plugins.Wikibear.timeoutSec` (global, PositiveInteger) — default `120` — How long to wait for the claude CLI to produce a factoid.

## YouTube

Snarfer for YouTube video URLs.

**Commands:**
- `!youtubeSnarfer`

**Config:**
- `supybot.plugins.YouTube.snarfer` (channel, Boolean) — default `True`
- `supybot.plugins.YouTube.bold` (channel, Boolean) — default `True`
- `supybot.plugins.YouTube.prefix` (channel, String) — default `_DEFAULT_PREFIX`
- `supybot.plugins.YouTube.maxHashtags` (channel, NonNegativeInteger) — default `4`
- `supybot.plugins.YouTube.timeout` (channel, PositiveInteger) — default `15`
- `supybot.plugins.YouTube.cookiesFile` (channel, String) — default `'/home/botuser/runbot/youtube-cookies.txt'`
- `supybot.plugins.YouTube.shrink` (channel, Boolean) — default `True`
- `supybot.plugins.YouTube.shrinkBold` (channel, Boolean) — default `True`
- `supybot.plugins.YouTube.shrinkShowDomain` (channel, Boolean) — default `False`
