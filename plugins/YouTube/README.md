# YouTube

Snarfs YouTube URLs and posts video metadata: title, uploader, duration, view count, upload date, and a shortened URL.

Handles `youtube.com/watch`, `youtu.be`, `/shorts/`, `/live/`, and `/embed/` links. Channel/playlist URLs are intentionally ignored and left to ShrinkUrl.

## Configuration

To avoid duplicate shortened URLs, configure ShrinkUrl to skip YouTube links:

```
!config channel #yourchannel plugins.ShrinkUrl.nonSnarfingRegexp (?i)(youtu\.be|youtube\.com)
```

## Requirements

- `yt-dlp` installed at `/usr/bin/yt-dlp`
- YouTube cookies at `/home/botuser/runbot/youtube-cookies.txt` (refresh every few weeks to avoid bot-detection)
