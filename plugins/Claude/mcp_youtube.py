#!/usr/bin/env python3
"""MCP server: fetch_transcript(url) for YouTube videos.

Two-step approach because Hetzner IPs trip YouTube's bot check, while
the WARP exit-node trips a 429 on /api/timedtext:

  1. yt-dlp through WARP socks5 proxy to fetch info-json (subtitle URLs
     + metadata). WARP gets past the bot check.
  2. Direct urlopen() from the server IP to the timedtext VTT URL.
     Server IP is fine for this endpoint; WARP is rate-limited there.

Returns plain text with timestamps and rolling-subtitle duplicates
stripped. Host-whitelisted to youtube.com / youtu.be.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

os.chdir(tempfile.gettempdir())

from mcp.server.fastmcp import FastMCP


_YTDLP = "/usr/bin/yt-dlp"
_PROXY = "socks5h://127.0.0.1:40000"
_TIMEOUT_META = 25.0
_TIMEOUT_SUB = 15.0
_MAX_CHARS = 8000
_MAX_SUB_BYTES = 4 * 1024 * 1024
_CACHE_TTL = 3600
_ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "youtu.be", "music.youtube.com",
}
_LANG_PREFS = ("en", "en-orig", "en-US", "en-GB", "de", "de-DE")

_TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->")
_TAG_RE = re.compile(r"<[^>]+>")

_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:generateContent"
)
_GEMINI_TIMEOUT = 90.0
_GEMINI_PROMPT = (
    "Watch this YouTube video and produce a compact, factual summary in plain text. "
    "Mention what kind of video it is, who/what is featured, what happens, and any "
    "notable specifics (key events, names, places, numbers, conclusions). "
    "Aim for 600-1500 characters. Plain text only — no markdown, no bullet points."
)


def _cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "claude-yt")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _sweep(d):
    now = time.time()
    try:
        for n in os.listdir(d):
            p = os.path.join(d, n)
            try:
                if now - os.path.getmtime(p) > _CACHE_TTL:
                    os.unlink(p)
            except OSError:
                pass
    except OSError:
        pass


def _host_ok(url):
    try:
        h = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return h in _ALLOWED_HOSTS


def _parse_vtt(text):
    lines_out = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "WEBVTT" or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if _TS_RE.match(line) or "-->" in line:
            continue
        if line.isdigit():
            continue
        clean = _TAG_RE.sub("", line).strip()
        if not clean:
            continue
        if clean in seen:
            continue
        seen.add(clean)
        lines_out.append(clean)
    return " ".join(lines_out)


def _pick_vtt_url(info):
    captions = {}
    for src in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        for lang, tracks in src.items():
            if lang not in captions:
                captions[lang] = tracks
    for lang in _LANG_PREFS:
        tracks = captions.get(lang)
        if not tracks:
            continue
        for t in tracks:
            if t.get("ext") == "vtt" and t.get("url"):
                return t["url"], lang
    for lang, tracks in captions.items():
        for t in tracks:
            if t.get("ext") == "vtt" and t.get("url"):
                return t["url"], lang
    return None, None


def _fetch_url(url, timeout, max_bytes):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise ValueError(f"HTTP {r.status}")
        data = r.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("subtitle exceeds size cap")
        return data.decode("utf-8", errors="replace")


def _describe_video_via_gemini(url, title):
    """Ask Gemini to watch the YouTube URL directly and return a plain-text
    description. Returns the text (with optional title header, capped) or
    None on any error."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"fileData": {"fileUri": url, "mimeType": "video/*"}},
                {"text": _GEMINI_PROMPT},
            ],
        }],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 800,
            "mediaResolution": "MEDIA_RESOLUTION_LOW",
        },
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{_GEMINI_ENDPOINT}?key={api_key}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_GEMINI_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            err_body = ""
        return None if not err_body else None
    except Exception:
        return None
    try:
        cands = payload.get("candidates") or []
        if not cands:
            return None
        parts = cands[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
    except Exception:
        return None
    if not text:
        return None
    header = f"[{title}] " if title else ""
    out = header + text
    if len(out) > _MAX_CHARS:
        out = out[:_MAX_CHARS].rsplit(" ", 1)[0] + " …[truncated]"
    return out


mcp = FastMCP("youtube")


@mcp.tool()
def fetch_transcript(url: str) -> str:
    """Fetch the transcript of a YouTube video as plain text.

    Returns the spoken text (auto-captions or uploaded subtitles, en/de
    preferred), with timestamps and duplicates stripped, capped at
    ~8000 characters. Returns an error string starting with 'error:'
    if no transcript is available or the URL is invalid.
    """
    if not _host_ok(url):
        return "error: only youtube.com / youtu.be URLs are accepted"

    cache = _cache_dir()
    _sweep(cache)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    cached = os.path.join(cache, f"{key}.txt")
    if os.path.exists(cached) and time.time() - os.path.getmtime(cached) < _CACHE_TTL:
        try:
            with open(cached, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            pass

    with tempfile.TemporaryDirectory(prefix="yt-") as work:
        cmd = [
            _YTDLP,
            "--proxy", _PROXY,
            "--skip-download",
            "--write-info-json",
            "--no-warnings",
            "--no-playlist",
            "-o", os.path.join(work, "%(id)s.%(ext)s"),
            url,
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=_TIMEOUT_META, check=False,
            )
        except subprocess.TimeoutExpired:
            return "error: yt-dlp timed out"
        except FileNotFoundError:
            return "error: yt-dlp not installed"

        info_path = None
        for name in os.listdir(work):
            if name.endswith(".info.json"):
                info_path = os.path.join(work, name)
                break

        title = ""
        info = None
        if info_path:
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                title = (info.get("title") or "").strip()
            except (OSError, json.JSONDecodeError):
                info = None

        text = None
        if info is not None:
            vtt_url, _lang = _pick_vtt_url(info)
            if vtt_url:
                try:
                    raw = _fetch_url(vtt_url, _TIMEOUT_SUB, _MAX_SUB_BYTES)
                    body = _parse_vtt(raw)
                    if body:
                        header = f"[{title}] " if title else ""
                        text = header + body
                        if len(text) > _MAX_CHARS:
                            text = text[:_MAX_CHARS].rsplit(" ", 1)[0] + " …[truncated]"
                except Exception:
                    text = None

        if text is None:
            text = _describe_video_via_gemini(url, title)
        if text is None:
            stderr = (r.stderr or "").strip().splitlines()
            hint = stderr[-1] if stderr else "no transcript and video analysis unavailable"
            return f"error: {hint[:200]}"

        try:
            tmp = cached + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, cached)
        except OSError:
            pass
        return text


if __name__ == "__main__":
    mcp.run()
