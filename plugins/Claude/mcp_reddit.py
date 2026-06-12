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

import base64
import json
import os
import shutil
import subprocess
import sys
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

# --- remaster_video pipeline (frame -> Claude-analysed Flux Kontext -> Atlas) -
# Claude Opus 4.8 vision-analyses the frame (anchor), Flux Kontext [max] does a
# subject-PRESERVING up-res edit (so a "catfish" meme stays a catfish instead of
# becoming a real cat the way free recreation did), then Seedance 2.0 animates.
_RUNWARE_ENDPOINT = "https://api.runware.ai/v1"
_KONTEXT_MODEL = "bfl:4@1"          # FLUX.1 Kontext [max] — premium edit/up-res
_KONTEXT_TIMEOUT = 120.0
_CLAUDE_BIN = "/home/botuser/.local/bin/claude"
_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
_CLAUDE_ANALYZE_TIMEOUT = 90.0
_ANALYZE_SYS = (
    "You are inspecting ONE still image frame so (a) an image model can up-res it "
    "WITHOUT changing what it depicts and (b) an image-to-video model can bring it "
    "to life. Output EXACTLY two lines, no markdown, no preamble:\n"
    "DESCRIPTION: <under 80 words — the exact main subject, any on-screen "
    "text/caption verbatim, the medium (real photo, meme, screenshot, cartoon, "
    "CGI/render), and key visual details. Only what is literally visible; never "
    "invent or 'improve' content.>\n"
    "MOTION: <under 30 words — concrete, lively movement to animate this scene: "
    "what each subject actually does, plus ambient motion (water, bubbles, hair, "
    "cloth, light). Be specific and visible — NOT 'subtle' or 'gentle'.>"
)
_ATLAS_ENDPOINT = "https://api.atlascloud.ai/api/v1/model/generateVideo"
_ATLAS_POLL_BASE = "https://api.atlascloud.ai/api/v1/model/prediction/"
_ATLAS_I2V_MODEL = "bytedance/seedance-2.0/image-to-video"   # !video-tier model
_ATLAS_I2V_FALLBACK = "atlascloud/wan-2.2-turbo-spicy/image-to-video"
_ATLAS_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_ATLAS_TIMEOUT = 300.0
_DEFAULT_MOTION = ("lively natural movement: the subjects move, sway and shift "
                   "visibly, water ripples and bubbles rise, smooth cinematic motion")
_ENHANCE_BASE = (
    "Produce a significantly higher-quality, sharper, cleaner, higher-resolution "
    "version of the reference image. Keep the subject, any text/captions, layout, "
    "composition, framing and colours EXACTLY as they are — do not add, remove, "
    "replace or reinterpret anything in the picture. Only improve fidelity, "
    "lighting, sharpness, texture and fine detail. "
)
# Hosts the remaster tool will download from (reddit set + YouTube).
_REMASTER_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "music.youtube.com",
}


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


def _remaster_host_ok(url):
    if _host_ok(url):
        return True
    try:
        h = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return h in _REMASTER_HOSTS or h.endswith(".youtube.com")


def _extract_frame(media, work):
    """Pull one representative ('key') frame out of the clip as PNG. Uses
    ffmpeg's thumbnail filter (picks the most representative frame from a
    window); falls back to the first decodable frame. Returns the PNG path."""
    frame = os.path.join(work, "frame.png")
    base = [_FFMPEG, "-y", "-loglevel", "error", "-i", media]
    for vf in (["-vf", "thumbnail", "-frames:v", "1"], ["-frames:v", "1"]):
        try:
            subprocess.run(base + vf + [frame], capture_output=True,
                           timeout=120, check=False)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise RuntimeError("ffmpeg frame extraction failed: %s" % e)
        if os.path.exists(frame) and os.path.getsize(frame):
            return frame
    raise RuntimeError("could not extract a frame from the video")


def _png_dims(raw):
    """(width, height) from a PNG IHDR, else None."""
    if len(raw) >= 24 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return (int.from_bytes(raw[16:20], "big"),
                    int.from_bytes(raw[20:24], "big"))
        except Exception:
            return None
    return None


# FLUX Kontext only accepts these exact width/height pairs; any other size →
# Runware 400 unsupportedFluxKontextDimensions. Snap to the closest aspect.
_KONTEXT_DIMS = [
    (1568, 672), (1392, 752), (1248, 832), (1184, 880), (1024, 1024),
    (880, 1184), (832, 1248), (752, 1392), (672, 1568),
]


def _kontext_dims(w, h):
    """Pick the supported FLUX Kontext size whose aspect ratio is closest to
    the source frame (Kontext rejects anything off its fixed list)."""
    if not w or not h or w <= 0 or h <= 0:
        return 1024, 1024
    ar = w / h
    def closeness(d):
        r = ar / (d[0] / d[1])
        return max(r, 1.0 / r)
    return min(_KONTEXT_DIMS, key=closeness)


def _claude_analyze_frame(path):
    """Claude Opus 4.8 looks at the extracted frame (via the Read tool) and
    returns (description, motion): a faithful description that anchors the
    up-res edit, and a concrete motion line to drive the i2v step. Best-effort:
    returns ('', '') on any failure."""
    env = dict(os.environ)
    env.setdefault("HOME", "/home/botuser")
    env.setdefault("CLAUDE_CONFIG_DIR", "/home/botuser/runbot/.claude")
    cmd = [
        _CLAUDE_BIN, "-p", "--model", _CLAUDE_MODEL,
        "--no-session-persistence", "--disable-slash-commands",
        "--allowedTools", "Read",
        "--append-system-prompt", _ANALYZE_SYS,
    ]
    inp = ("Use the Read tool to open the image at %s, then analyse it exactly "
           "as instructed." % path)
    try:
        r = subprocess.run(cmd, input=inp, capture_output=True, text=True,
                           timeout=_CLAUDE_ANALYZE_TIMEOUT, env=env)
    except Exception as e:
        print("claude analyze failed: %s" % e, file=sys.stderr)
        return "", ""
    if r.returncode != 0:
        print("claude analyze exit %d: %s" % (
            r.returncode, (r.stderr or "")[:200]), file=sys.stderr)
        return "", ""
    out = (r.stdout or "").strip()
    desc, motion = "", ""
    for line in out.splitlines():
        up = line.strip().upper()
        if up.startswith("DESCRIPTION:"):
            desc = line.split(":", 1)[1].strip()
        elif up.startswith("MOTION:"):
            motion = line.split(":", 1)[1].strip()
    if not desc:                       # model ignored the format — use it all
        desc = out
    return desc[:700], motion[:300]


def _runware_kontext(prompt, ref_url, width, height, api_key):
    """FLUX.1 Kontext [max] subject-preserving up-res edit of ref_url. Returns
    the Runware image URL or raises RuntimeError. Kontext rejects steps/CFG."""
    task = {
        "taskType": "imageInference",
        "taskUUID": str(uuid.uuid4()),
        "positivePrompt": prompt[:2900],
        "model": _KONTEXT_MODEL,
        "referenceImages": [ref_url],
        "width": width,
        "height": height,
        "numberResults": 1,
        "outputType": "URL",
    }
    req = urllib.request.Request(
        _RUNWARE_ENDPOINT,
        data=json.dumps([task]).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_KONTEXT_TIMEOUT) as resp:
            j = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RuntimeError("kontext http %d%s" % (
            e.code, ": " + detail if detail else ""))
    except Exception as e:
        raise RuntimeError("kontext request failed: %s" % e)
    errs = j.get("errors") or []
    if errs:
        raise RuntimeError("kontext: %s" % (
            errs[0].get("message") or errs[0].get("code") or str(errs[0]))[:200])
    results = j.get("data") or []
    if not results or "imageURL" not in results[0]:
        raise RuntimeError("kontext returned no imageURL")
    return results[0]["imageURL"]


def _zipline_upload_bytes(raw, ext, content_type):
    """Upload raw bytes to Zipline; returns the public URL or raises."""
    token = os.environ.get("ZIPLINE_TOKEN")
    endpoint = os.environ.get("ZIPLINE_UPLOAD_URL")
    host = os.environ.get("ZIPLINE_HOST")
    if not token or not endpoint:
        raise RuntimeError("hosting unavailable (ZIPLINE_TOKEN/ZIPLINE_UPLOAD_URL not set)")
    boundary = "----fatbot" + uuid.uuid4().hex
    head = (
        "--" + boundary + "\r\n"
        'Content-Disposition: form-data; name="file"; filename="%s.%s"\r\n'
        "Content-Type: %s\r\n\r\n" % (uuid.uuid4().hex, ext, content_type)
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


def _atlas_i2v(prompt, image_url, api_key, model=_ATLAS_I2V_MODEL):
    """Animate a still image into a ~5s clip via Atlas image-to-video.
    Returns the Atlas video URL or raises RuntimeError."""
    payload = {
        "model": model,
        "prompt": prompt,
        "image": image_url,
        "duration": 5,
    }
    req = urllib.request.Request(
        _ATLAS_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _ATLAS_UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            j = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError("atlas http %d: %s" % (e.code, detail))
    except Exception as e:
        raise RuntimeError("atlas request failed: %s" % e)
    pred = j.get("data") or j
    pred_id = pred.get("id")
    if not pred_id:
        raise RuntimeError("atlas: no prediction id in response")
    poll_headers = {
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "User-Agent": _ATLAS_UA,
    }
    deadline = time.time() + _ATLAS_TIMEOUT
    while time.time() < deadline:
        time.sleep(5)
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    _ATLAS_POLL_BASE + pred_id, headers=poll_headers,
                    method="GET"), timeout=30) as resp:
                pj = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print("atlas poll error: %s" % e, file=sys.stderr)
            continue
        d = pj.get("data") or pj
        status = (d.get("status") or "").lower()
        if status in ("completed", "succeeded", "success"):
            outs = d.get("outputs") or d.get("output") or []
            if isinstance(outs, str):
                return outs
            if outs and isinstance(outs, list):
                first = outs[0]
                if isinstance(first, str):
                    return first
                if isinstance(first, dict):
                    for key in ("url", "video", "videoURL"):
                        if key in first:
                            return first[key]
            raise RuntimeError("atlas: completed but no output url")
        if status in ("failed", "error", "cancelled"):
            raise RuntimeError("atlas failed: %s" % (
                d.get("error") or d.get("message") or "unknown"))
    raise RuntimeError("atlas: timeout after %ds" % int(_ATLAS_TIMEOUT))


def _download_to(url, path):
    """Download a remote file (the Atlas mp4) to disk for re-hosting."""
    req = urllib.request.Request(url, headers={"User-Agent": _ATLAS_UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:
        raise RuntimeError("fetching the generated video failed: %s" % e)
    if not os.path.getsize(path):
        raise RuntimeError("generated video was empty")


mcp = FastMCP("reddit")


@mcp.tool()
def remaster_video(url: str, instruction: str = "", motion: str = "") -> str:
    """Download a video, then make a higher-quality re-animated version of it.

    Pipeline: download the clip and take a representative key frame; Claude
    Opus 4.8 vision-analyses that frame so the up-res stays faithful to what's
    actually there; FLUX.1 Kontext [max] does a subject-PRESERVING higher-
    quality edit of the frame (it keeps the subject/text instead of inventing a
    new one); then Seedance 2.0 animates that enhanced still into a short (~5s)
    clip hosted on img.example.net.

    Use this when the user asks to download a video AND make a better /
    higher-quality / remastered / upscaled version of it. Accepts Reddit
    (reddit.com / redd.it / v.redd.it / redgifs) and YouTube URLs.

    NOTE: the result is a NEW short clip whose motion is freshly generated —
    it will NOT replay the original action frame-for-frame. Tell the user that.

    `instruction`: extra quality wishes (e.g. 'cinematic, film grain').
    `motion`: how the re-animated clip should move (optional).

    Returns lines including 'Remastered: <video url>' and 'Enhanced still:
    <image url>', or a string starting with 'error:' on failure.
    """
    if not _remaster_host_ok(url):
        return "error: only Reddit (reddit.com / redd.it / v.redd.it / redgifs) and YouTube URLs are accepted"
    runware_key = os.environ.get("RUNWARE_API_KEY")
    if not runware_key:
        return "error: RUNWARE_API_KEY not configured"
    atlas_key = os.environ.get("ATLASCLOUD_API_KEY")
    if not atlas_key:
        return "error: ATLASCLOUD_API_KEY not configured"

    with tempfile.TemporaryDirectory(prefix="remaster-") as work:
        try:
            media, title = _download(url, work)
            frame = _extract_frame(media, work)
            with open(frame, "rb") as f:
                raw = f.read()
            # Host the raw frame so Kontext can reference it by URL.
            frame_url = _zipline_upload_bytes(raw, "png", "image/png")
            width, height = _kontext_dims(*( _png_dims(raw) or (1024, 1024) ))
            # Claude Opus 4.8 anchors the edit to the real content AND suggests
            # concrete motion for the i2v step (best-effort).
            analysis, motion_hint = _claude_analyze_frame(frame)
            prompt = _ENHANCE_BASE
            if analysis:
                prompt += "The image shows: " + analysis + " "
            if instruction:
                prompt += instruction.strip()
            enhanced_url = _runware_kontext(prompt, frame_url, width, height,
                                            runware_key)
            enh_path = os.path.join(work, "enhanced.png")
            _download_to(enhanced_url, enh_path)
            with open(enh_path, "rb") as f:
                enhanced_raw = f.read()
            still_url = _zipline_upload_bytes(enhanced_raw, "png", "image/png")
            # Animate via Seedance 2.0, falling back to Wan turbo-spicy. Prefer
            # an explicit motion arg, else Opus's motion suggestion, else a
            # lively generic default (never the old 'subtle' one that came out
            # near-static).
            motion_prompt = motion or motion_hint or instruction or _DEFAULT_MOTION
            try:
                atlas_url = _atlas_i2v(motion_prompt, still_url, atlas_key)
            except RuntimeError as e:
                print("seedance failed (%s); falling back to wan" % e,
                      file=sys.stderr)
                atlas_url = _atlas_i2v(motion_prompt, still_url, atlas_key,
                                       model=_ATLAS_I2V_FALLBACK)
            out_mp4 = os.path.join(work, "remastered.mp4")
            _download_to(atlas_url, out_mp4)
            hosted = _zipline_upload(out_mp4)
        except RuntimeError as e:
            return "error: %s" % e

    out = []
    if title:
        out.append("[%s]" % title)
    out.append("Remastered: %s" % hosted)
    out.append("Enhanced still: %s" % still_url)
    out.append("(new clip — motion is freshly generated, not the original action)")
    return "\n".join(out)


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
