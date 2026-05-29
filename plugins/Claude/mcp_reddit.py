"""Reddit video MCP server for the Claude plugin.

Unlike YouTube (which Gemini can fetch natively by URL), Reddit videos
(v.redd.it) serve audio and video as separate DASH streams and cannot be
referenced by fileUri. So we:

  1. yt-dlp downloads + ffmpeg-merges the clip into a single mp4 (capped
     resolution/size) in a temp dir.
  2. Upload that mp4 to the Gemini Files API and ask gemini-2.5-flash to
     describe it (plain text). Handles any size up to the Files API limit,
     so no inline-base64 size cliff.
  3. Optionally re-host the mp4 on our Zipline image host (same multipart
     upload the Create plugin uses) and return the public img.example.net URL.

Host-whitelisted to reddit / redd.it / v.redd.it / redgifs. All failures
return a string starting with 'error:'.
"""

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

os.chdir(tempfile.gettempdir())

from mcp.server.fastmcp import FastMCP


_YTDLP = "/usr/bin/yt-dlp"
_FFMPEG = "/usr/bin/ffmpeg"
# reddit-cookies.txt lives in the runbot root (this file is at
# <runbot>/plugins/Claude/mcp_reddit.py). Reddit now requires account auth
# for its metadata API from datacenter IPs, so logged-in cookies are needed.
_COOKIES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "reddit-cookies.txt",
)
_DL_TIMEOUT = 600.0
_MAX_CHARS = 8000

_ALLOWED_HOSTS = {
    "reddit.com", "www.reddit.com", "old.reddit.com", "m.reddit.com",
    "np.reddit.com", "redd.it", "v.redd.it", "i.redd.it",
    "redgifs.com", "www.redgifs.com",
}

# Tried in order — if the primary is overloaded (503) we fall back to the
# next, since demand spikes usually hit one model at a time.
_GEMINI_MODELS = ("gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite")
_GEMINI_FILES_BASE = "https://generativelanguage.googleapis.com"


def _gemini_endpoint(model):
    return f"{_GEMINI_FILES_BASE}/v1beta/models/{model}:generateContent"
_GEMINI_TIMEOUT = 90.0
_FILES_ACTIVE_TIMEOUT = 120.0
_GEMINI_PROMPT = (
    "Watch this short video (downloaded from Reddit) and produce a compact, "
    "factual summary in plain text. Mention what kind of clip it is, who/what "
    "is featured, what happens, and any notable specifics (key events, on-screen "
    "text, spoken words, sounds, conclusions). Aim for 400-1200 characters. "
    "Plain text only — no markdown, no bullet points."
)


def _host_ok(url):
    try:
        h = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if h in _ALLOWED_HOSTS:
        return True
    return h.endswith(".reddit.com") or h.endswith(".redd.it") or h.endswith(".redgifs.com")


def _download(url, work):
    """yt-dlp download + ffmpeg merge into a single mp4. Returns (path, title)
    or raises RuntimeError with a user-facing message."""
    cmd = [
        _YTDLP,
        "--no-playlist", "--no-warnings",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--ffmpeg-location", _FFMPEG,
        "--write-info-json",
        "-o", os.path.join(work, "clip.%(ext)s"),
    ]
    if os.path.exists(_COOKIES):
        cmd += ["--cookies", _COOKIES]
    cmd.append(url)
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_DL_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("yt-dlp timed out downloading the video")
    except FileNotFoundError:
        raise RuntimeError("yt-dlp not installed")

    title = ""
    media = None
    for name in sorted(os.listdir(work)):
        full = os.path.join(work, name)
        if name.endswith(".info.json"):
            try:
                with open(full, "r", encoding="utf-8") as f:
                    title = (json.load(f).get("title") or "").strip()
            except (OSError, json.JSONDecodeError):
                pass
            continue
        if name.endswith(".part") or name.endswith(".ytdl"):
            continue
        media = full
    if not media or not os.path.getsize(media):
        stderr = (r.stderr or "").strip().splitlines()
        hint = stderr[-1] if stderr else "no downloadable video found at that URL"
        raise RuntimeError(hint[:200])
    return media, title


def _gemini_describe(path, api_key):
    """Upload the file to the Gemini Files API, wait until ACTIVE, then ask
    gemini to describe it. Returns plain text or raises RuntimeError."""
    size = os.path.getsize(path)
    mime = "video/mp4"

    # 1. resumable upload — start
    start = urllib.request.Request(
        f"{_GEMINI_FILES_BASE}/upload/v1beta/files?key={api_key}",
        data=json.dumps({"file": {"display_name": "reddit-clip"}}).encode("utf-8"),
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(start, timeout=30) as r:
            upload_url = r.headers.get("X-Goog-Upload-URL")
    except urllib.error.HTTPError as e:
        raise RuntimeError("gemini files start http %d" % e.code)
    if not upload_url:
        raise RuntimeError("gemini files: no upload url returned")

    # 2. upload + finalize
    with open(path, "rb") as f:
        blob = f.read()
    up = urllib.request.Request(
        upload_url, data=blob,
        headers={
            "Content-Length": str(size),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(up, timeout=180) as r:
            j = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("gemini files upload http %d" % e.code)
    finfo = j.get("file", {})
    name = finfo.get("name")
    uri = finfo.get("uri")
    state = finfo.get("state")
    if not uri or not name:
        raise RuntimeError("gemini files: malformed upload response")

    # 3. poll until ACTIVE
    deadline = time.time() + _FILES_ACTIVE_TIMEOUT
    while state and state != "ACTIVE":
        if state == "FAILED":
            raise RuntimeError("gemini could not process the video (FAILED)")
        if time.time() > deadline:
            raise RuntimeError("gemini file processing timed out")
        time.sleep(2.0)
        g = urllib.request.Request(f"{_GEMINI_FILES_BASE}/v1beta/{name}?key={api_key}")
        try:
            with urllib.request.urlopen(g, timeout=30) as r:
                jj = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError("gemini files poll http %d" % e.code)
        state = jj.get("state")
        uri = jj.get("uri") or uri

    # 4. generateContent
    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"fileData": {"fileUri": uri, "mimeType": mime}},
                {"text": _GEMINI_PROMPT},
            ],
        }],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 800,
            "mediaResolution": "MEDIA_RESOLUTION_LOW",
            # Disable "thinking" so newer models don't burn the token budget
            # on reasoning or leak it into the output.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    data = json.dumps(body).encode("utf-8")
    # 503 ("model overloaded") and 429 are common + transient — retry each
    # model with backoff, then fall back to the next model in the list.
    payload = None
    last_err = "gemini generate failed"
    for model in _GEMINI_MODELS:
        for attempt in range(3):
            req = urllib.request.Request(
                f"{_gemini_endpoint(model)}?key={api_key}",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=_GEMINI_TIMEOUT) as r:
                    payload = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:160]
                except Exception:
                    pass
                last_err = "gemini generate http %d (%s)%s" % (
                    e.code, model, ": " + detail if detail else "")
                if e.code in (429, 500, 503) and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                break  # non-retryable or attempts exhausted → next model
            except urllib.error.URLError as e:
                last_err = "gemini generate connection error (%s): %s" % (model, e)
                if attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                break
        if payload is not None:
            break
    if payload is None:
        raise RuntimeError(last_err)

    cands = payload.get("candidates") or []
    if not cands:
        raise RuntimeError("gemini returned no description")
    parts = cands[0].get("content", {}).get("parts") or []
    # Skip "thought" parts so thinking-model reasoning never leaks into output.
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise RuntimeError("gemini returned an empty description")
    return text


def _zipline_upload(path):
    """Re-host the mp4 on Zipline. Returns the public URL or raises
    RuntimeError. Reuses the same env + multipart scheme as the Create plugin."""
    token = os.environ.get("ZIPLINE_TOKEN")
    endpoint = os.environ.get("ZIPLINE_UPLOAD_URL")
    host = os.environ.get("ZIPLINE_HOST")
    if not token or not endpoint:
        raise RuntimeError("hosting unavailable (ZIPLINE_TOKEN/ZIPLINE_UPLOAD_URL not set)")
    with open(path, "rb") as f:
        raw = f.read()
    boundary = "----fatbot" + uuid.uuid4().hex
    head = (
        "--" + boundary + "\r\n"
        'Content-Disposition: form-data; name="file"; filename="%s.mp4"\r\n'
        "Content-Type: video/mp4\r\n\r\n" % uuid.uuid4().hex
    ).encode("utf-8")
    tail = ("\r\n--" + boundary + "--\r\n").encode("utf-8")
    data = head + raw + tail
    headers = {
        "authorization": token,
        "content-type": "multipart/form-data; boundary=" + boundary,
    }
    if host:
        headers["Host"] = host
    req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            j = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("hosting failed (zipline http %d)" % e.code)
    except Exception as e:
        raise RuntimeError("hosting failed (%s)" % e)
    files = j.get("files") or []
    if not files or "url" not in files[0]:
        raise RuntimeError("hosting failed (no url in zipline response)")
    url = files[0]["url"]
    base = os.environ.get("ZIPLINE_PUBLIC_BASE")
    if base:
        url = base.rstrip("/") + urllib.parse.urlsplit(url).path
    return url


mcp = FastMCP("reddit")


@mcp.tool()
def analyze_reddit_video(url: str, upload_to_host: bool = False) -> str:
    """Download a Reddit video, watch it, and return a plain-text description.

    Use this for Reddit links (reddit.com / redd.it / v.redd.it / redgifs)
    when the user wants the video watched, analyzed, summarized, or saved.
    Set upload_to_host=True ONLY when the user explicitly asks to host /
    upload / save the clip; in that case a 'Hosted: <url>' line with a
    public img.example.net URL is appended.

    Returns the description (optionally with a Hosted line), or a string
    starting with 'error:' on failure.
    """
    if not _host_ok(url):
        return "error: only reddit.com / redd.it / v.redd.it / redgifs URLs are accepted"

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "error: GEMINI_API_KEY not configured"

    desc = None
    desc_err = None
    hosted = None
    host_err = None
    with tempfile.TemporaryDirectory(prefix="reddit-") as work:
        try:
            media, title = _download(url, work)
        except RuntimeError as e:
            return "error: %s" % e

        try:
            desc = _gemini_describe(media, api_key)
        except RuntimeError as e:
            desc_err = str(e)

        if upload_to_host:
            try:
                hosted = _zipline_upload(media)
            except RuntimeError as e:
                host_err = str(e)

    if desc is None and not hosted:
        # Nothing worked.
        return "error: %s" % (desc_err or "analysis failed")

    out = []
    if title:
        out.append("[%s]" % title)
    if desc:
        out.append(desc)
    elif desc_err:
        out.append("(analysis unavailable: %s)" % desc_err)
    if hosted:
        out.append("Hosted: %s" % hosted)
    elif upload_to_host and host_err:
        out.append("(%s)" % host_err)

    text = "\n".join(out).strip()
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS].rsplit(" ", 1)[0] + " …[truncated]"
    return text


if __name__ == "__main__":
    mcp.run()
