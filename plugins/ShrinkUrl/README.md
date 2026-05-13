# ShrinkUrl

URL snarfer that automatically shortens long URLs posted in the channel. Strips tracking parameters via `unalix` before shortening and appends `(untracked, at <domain>)` when parameters were removed.

This is a modified version of the stock Limnoria ShrinkUrl plugin. Key changes:

- **t.ly shortener** — uses the t.ly API (Bearer token) instead of the default shorteners.
- **unalix cleaning** — tracking/UTM parameters are stripped before the URL is submitted.
- **is.gd WARP routing** — is.gd traffic is tunnelled through Cloudflare WARP (SOCKS5 on `127.0.0.1:40000`) to work around Cloudflare challenges on the VPS egress IP. No other traffic uses WARP.

## Requirements

- `TLY_API_TOKEN` environment variable
- `unalix` Python package (`pip install unalix`)
- Cloudflare WARP running in proxy mode on port 40000 (for is.gd only)
