# Title

URL snarfer that automatically fetches and posts the `<title>` (or `og:title`) of web pages linked in the channel.

## Features

- Skips obvious binary/media URLs (images, video, audio, archives, PDFs) by extension and Content-Type.
- Uses `curl_cffi` with Chrome impersonation to bypass bot-detection on sites like Reddit.
- Handles Reddit's `shreddit-post` custom element for post titles.
- Falls back to `og:title` when `<title>` is missing or unhelpful.
- Strips trailing punctuation and decodes HTML entities.
- Per-channel enable/disable via `plugins.Title.enable`.

## Requirements

- `curl_cffi` Python package (`pip install curl-cffi`) — falls back to `urllib` if unavailable.
