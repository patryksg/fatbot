# fatbot

Limnoria IRC bot (`fatbot`) running on **skund**. Connects to efnet
(`efnet.tngnet.nl`), joins `#fatkids`, `#oldnews`, `#testing123`.

## Layout

- Bot home: `/home/fatbot/runbot/`
- Master config: `fatbot.conf` (rewritten on shutdown — see "conf-rewrite gotcha")
- Per-section conf: `conf/{channels,users,networks}.conf` (also rewritten on shutdown)
- Custom plugins: `plugins/<Name>/`
- Channel logs: `logs/ChannelLogger/<network>/<#channel>/`
- Cookie jars (mode 600): `reddit-cookies.txt`, `youtube-cookies.txt` (see "Session cookies" below)
- Channel digest: `fatkidsinfo.md` (nightly summary of `#fatkids`)

## Limnoria install

Pipx-managed git master:

- Venv: `/home/fatbot/.local/share/pipx/venvs/limnoria/`
- Binary: `/home/fatbot/.local/bin/supybot`
- Update: `sudo -u fatbot pipx install --force git+https://github.com/ProgVal/Limnoria.git`
  (re-pulls master; `pipx upgrade limnoria` works once a release tag bumps)
- **Re-inject after `--force`:** the venv is recreated, so any injected
  packages must be re-added (see Dependencies).

The Debian apt `limnoria` package is also installed but unused — the
systemd unit points at the pipx binary.

## Dependencies

### Python packages (injected into the pipx venv)

```
sudo -u fatbot pipx inject limnoria curl_cffi
sudo -u fatbot pipx inject limnoria PySocks --pip-args="--no-deps"
```

- **`curl_cffi`** — used by the Title plugin's HTTP layer to impersonate
  `chrome131` and bypass TLS-fingerprint bot detection (Akamai,
  Cloudflare). Single shared `cc.Session()`, cookie jar persistence, SSRF
  guard via `getaddrinfo`.
- **`PySocks`** — required for `socks5h://` URLs in requests/urllib, used
  by ShrinkUrl's `_getIsgdUrl` to route through cloudflare-warp.

### System: cloudflare-warp (SOCKS5 proxy mode)

Debian package `cloudflare-warp` (repo
`https://pkg.cloudflareclient.com bookworm main`, signed-by
`/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg`).

**Runs in SOCKS5 proxy mode on `127.0.0.1:40000`, NOT as a default
route.** A default-route ("warp" mode) install will kill SSH because
warp injects a tunnel interface and reroutes all egress.

**Why:** is.gd is fronted by Cloudflare's anti-bot challenge and blocks
the VPS egress IP with HTTP 403. Routing only is.gd traffic through
WARP returns the actual short URL. Everything else (t.ly, reddit,
fxtwitter, ...) continues to use the normal VPS egress.

**Setup / recovery sequence:**

```
apt install cloudflare-warp
warp-cli --accept-tos registration new
warp-cli --accept-tos mode proxy           # MUST be set before connect
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect
```

**Verify:**

- `warp-cli --accept-tos status` → `Connected`
- `warp-cli --accept-tos settings` → `Mode: WarpProxy on port 40000`
- `ip route` default still `via 172.31.1.1 dev eth0`; no `CloudflareWARP`
  interface in `ip link`
- `ss -tlnp | grep 40000` shows the local SOCKS5 listener
- `curl -x socks5h://127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace`
  returns `warp=on`; direct `curl` returns `warp=off`

**Persistence:** `warp-svc.service` is enabled; `Always On: true` in
settings; `reg.json` + `settings.json` in `/var/lib/cloudflare-warp/`
persist registration and mode across reboots.

**Consumer:** only `plugins/ShrinkUrl/plugin.py::_getIsgdUrl`. If WARP
is disconnected, that path falls back to tinyurl via direct egress —
the bot stays functional, just no is.gd links.

## systemd

- Unit: `/etc/systemd/system/fatbot.service`
- Drop-ins: `/etc/systemd/system/fatbot.service.d/{hardening,claude}.conf`
- Control: `sudo systemctl {start,stop,restart,status} fatbot`
- Logs: `sudo journalctl -u fatbot -f` and
  `/home/fatbot/runbot/logs/{messages,error}.log`

### Sandbox

`ProtectHome=read-only` with `ReadWritePaths=/home/fatbot/runbot`,
`PrivateTmp=true`. Subprocesses allowed (yt-dlp, claude). Hardening:
caps dropped, seccomp `@system-service ~@privileged`, MDWE,
PrivateDevices, ProtectProc=invisible. Score ~1.5 via
`systemd-analyze security`.

**Seccomp gotcha:** do NOT add `~@resources` to `SystemCallFilter` — the
bundled `claude` binary (V8 JIT) needs scheduling/rlimit syscalls and
SIGSYS-crashes on startup if they're blocked.

**XDG redirect** for subprocess plugins writing to `$HOME`: pass
`XDG_CACHE_HOME=/home/fatbot/runbot/.cache`,
`XDG_CONFIG_HOME=/home/fatbot/runbot/.config`,
`CLAUDE_CONFIG_DIR=/home/fatbot/runbot/.claude` so writes stay inside
`ReadWritePaths`.

## conf-rewrite gotcha

supybot rewrites `fatbot.conf` *and* `conf/*.conf` on shutdown with its
in-memory state. To edit any of those:

1. `systemctl stop fatbot`
2. edit
3. `systemctl start fatbot`

A `restart` after editing while the bot is running silently reverts the
edits. Plugin code (`plugins/<Name>/plugin.py`) is safe to edit live —
those aren't rewritten:

```
rm -rf plugins/<Name>/__pycache__
systemctl restart fatbot     # or `!reload <Name>` from IRC
```

## Plugins

Custom plugins live in `plugins/`. Each plugin must be added to
`fatbot.conf`:

- `supybot.plugins: ... <Name>` (space-separated, ~line 842)
- `supybot.plugins.<Name>: True`

ChannelLogger (stock) is also loaded but does not appear in the
`supybot.plugins:` line.

Custom plugins currently installed:

- **ChanModes** — auto-asserts a configured mode string when bot has +o
- **Claude** — Claude-API-backed chat / Q&A; see fatbot-claude memory
- **Greeter**, **Hamster**, **InfoToggle**, **NuWeather**, **Relay**,
  **YouTube** — utility plugins
- **ShrinkUrl** — overridden to default to is.gd via cloudflare-warp
  SOCKS5; falls back to tinyurl
- **Title** — URL-title snarfer using `curl_cffi`; combined-mode with
  ShrinkUrl posts `<short> | <title>`; reddit/twitter special-cased
  (rewrites to `old.reddit.com`, uses `api.fxtwitter.com`)

## Session cookies (reddit, YouTube)

Two plugins read **Netscape-format `cookies.txt`** files containing
live session cookies from a logged-in browser. These are required
because both sites now block anonymous requests from VPS egress IPs;
TLS-fingerprint impersonation alone is not enough.

| File | Consumer | Config key | Why |
| --- | --- | --- | --- |
| `reddit-cookies.txt` | Title plugin (HTTP fetch path) | `supybot.plugins.Title.cookiesFile` | Reddit blocks unauthenticated requests from the VPS IP. With a `reddit_session` cookie, `old.reddit.com` returns a normal page (the new app's title only appears at ~430 KB offset, past our 256 KB cap — so the plugin rewrites `(www.)?reddit.com` → `old.reddit.com` before fetching). |
| `youtube-cookies.txt` | YouTube plugin (yt-dlp subprocess) | `supybot.plugins.YouTube.cookiesFile` | yt-dlp otherwise hits "Sign in to confirm you're not a bot" on metadata extraction. A logged-in cookie jar bypasses that gate. |

**How to provide them:**

1. In a logged-in browser session, export `cookies.txt` for the
   target domain (browser extension or `yt-dlp --cookies-from-browser`).
2. Save with mode 600, owned by the bot user.
3. Point the config keys at the absolute path.

The Title plugin auto-reloads its jar when the file's mtime changes,
so refreshing cookies doesn't need a bot restart. Cookies expire —
re-export periodically.

**Security note:** these files contain live session tokens. Never
commit them. The bot's `.gitignore` excludes `*-cookies.txt`.

## Command prefix

`!` — configured via `supybot.reply.whenAddressedBy.chars`.
`supybot.reply.whenNotCommand: False` is set deliberately so the Claude
plugin's nick-addressed `doPrivmsg` doesn't produce "not a valid command"
noise.
