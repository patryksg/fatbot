"""
Create: image and video generation via Gemini + Runware.ai (Flux/Kontext) + Atlas Cloud (video).

!pic <prompt>             — Gemini Nano Banana → Zipline (SFW; retries then errors, no Flux)  (#chan,generative)
!pic <url> <edit>         — edit a linked image via Gemini (SFW) → Zipline                    (#chan,generative)
!picnsfw <prompt>         — Flux image (NSFW-capable)                                          (#chan,generative)
!picnsfw <url> <edit>     — edit via FLUX Kontext, NSFW fallback to Lustify img2img            (#chan,generative)
!video <prompt>           — Flux image + Atlas Wan 2.2 I2V                                     (#chan,generative)
!videonsfw <prompt>       — Flux image + Atlas Spicy I2V                                       (#chan,generative)
"""

import os
import re
import uuid
import json
import base64
import socket
import tempfile
import ipaddress
import threading
import subprocess
import urllib.parse
import urllib.request
import urllib.error

import supybot.conf as conf
import supybot.ircdb as ircdb
import supybot.world as world
import supybot.callbacks as callbacks
import supybot.ircutils as ircutils
from supybot.commands import wrap, optional

NSFW_PREFIX = ircutils.bold('[NSFW]')

RUNWARE_ENDPOINT = "https://api.runware.ai/v1"
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_IMAGE_MODEL + ":generateContent"
)
ATLAS_ENDPOINT = "https://api.atlascloud.ai/api/v1/model/generateVideo"
ATLAS_POLL_BASE = "https://api.atlascloud.ai/api/v1/model/prediction/"
ATLAS_I2V_MODEL = "atlascloud/wan-2.2-turbo-spicy/image-to-video"
ATLAS_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

SEED_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
DEFAULT_MOTION_PROMPT = "natural subtle motion, gentle ambient movement"
CLAUDE_BIN = "/home/botuser/.local/bin/claude"
CLAUDE_CONFIG_DIR = "/home/botuser/runbot/.claude"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_TIMEOUT = 30

ATLAS_LLM_ENDPOINT = "https://api.atlascloud.ai/v1/chat/completions"
ATLAS_LLM_MODEL = "xai/grok-4-fast-non-reasoning"
ATLAS_LLM_TIMEOUT = 30

REFUSAL_MARKERS = (
    "i'm not able", "i am not able", "i'm unable", "i am unable",
    "i can't", "i cannot", "i can not", "i won't", "i will not",
    "i'd be happy", "i would be happy", "i don't", "i do not",
    "sorry,", "sorry —", "sorry, but", "let me know", "happy to",
)

EXPAND_SYSTEM = (
    "You expand image generation prompts with photographic detail for a PHOTOREALISTIC result. "
    "The user gives a subject. OUTPUT ONLY comma-separated descriptive phrases to append to it. "
    "REQUIRED — every output must include all of these, and you must INVENT concrete, specific "
    "choices (never generic): "
    "(1) a specific real-world SETTING/background with environmental detail — a named kind of "
    "place, props, surfaces, what's in the foreground AND background, weather/time of day; "
    "(2) the subject's pose, wardrobe, and expression fitting that scene; "
    "(3) one camera+lens choice and one lighting setup. "
    "CRITICAL: VARY your choices every time — do NOT default to gyms, studios, '85mm f/1.4', "
    "'rim light', 'shallow depth of field', or 'kodak portra'. Pick a different location, lens, "
    "time of day, and lighting on each call so successive images look distinct, not samey. "
    "Ground everything in plausible reality: candid documentary realism, natural imperfections, "
    "authentic textures. "
    "Do NOT repeat, paraphrase, judge, or restate the user's subject. No quotes, no prefix, no "
    "explanation — just the comma-separated phrases. Aim for 400-700 characters."
)

# Turn a user's casual edit request into ONE explicit imperative instruction for
# an instruction-based editor (Gemini / FLUX Kontext). The editor sees the image
# itself, so we never describe it; a vague "give that man a halo" gets ignored, an
# explicit "Add a glowing halo above his head, keep everything else unchanged" lands.
EDIT_REPHRASE_SYSTEM = (
    "You rewrite a short image-edit request into ONE clear, explicit, imperative "
    "editing instruction for an instruction-based image editor. You are ONLY "
    "rewording text -- you will NOT be shown any image, and you must NEVER ask to "
    "see one or say you can't see it. Assume the image and its subject exist. Do "
    "NOT describe the existing image; only state the change. Start with an "
    "imperative verb (Add, Remove, Replace, Change, Make, Turn, Put). Name "
    "placement and appearance so it integrates naturally, and end with 'keep "
    "everything else unchanged'. Output ONLY the instruction: one line, no "
    "quotes, no preamble, no markdown. Max 220 characters."
)

EDIT_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
MEDIA_MAX_BYTES = 100 * 1024 * 1024  # 100 MB (video re-host cap)
_IMPERSONATE = "chrome131"
_MAX_HOPS = 5
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}

# FLUX Kontext only accepts these exact width/height pairs (one per aspect
# ratio); arbitrary sizes -> Runware 400 unsupportedFluxKontextDimensions.
KONTEXT_DIMS = [
    (1568, 672), (1392, 752), (1248, 832), (1184, 880), (1024, 1024),
    (880, 1184), (832, 1248), (752, 1392), (672, 1568),
]

# Appended to the ORIGINAL subject on a refusal-retry to nudge Gemini's SFW
# filter into accepting the prompt.
SFW_RETRY_HINT = (
    " (depict tastefully and wholesomely: fully clothed in modest everyday "
    "attire, no nudity, no sexualization, candid and respectful)"
)

# Vision captioning anchors a picnsfw img2img edit to the actual scene so the
# edit doesn't drift the composition/background or multiply the subjects (see
# _edit_pic_nsfw). Kept SHORT: SDXL's CLIP only reads ~77 tokens, so a long
# caption would push the nudity terms out of the effective window.
GEMINI_CAPTION_MODEL = "gemini-2.5-flash"
GEMINI_CAPTION_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_CAPTION_MODEL + ":generateContent"
)
CAPTION_PROMPT = (
    "Describe this photo for an image generator in ONE concise sentence, max 30 "
    "words: number of people and body type, their pose, the setting, the single "
    "most distinctive background feature, and the lighting. Do not mention "
    "clothing. Output only the description."
)
# img2img negatives. BASE stops the seed from exploding into a crowd (the classic
# failure mode); CLOTHING is added for undress requests so the garment is removed
# at a lower, higher-fidelity strength.
EDIT_NEG_BASE = (
    "crowd, group of people, extra people, many people, multiple people, "
    "duplicated people, deformed, extra limbs, mutated hands, bad anatomy, "
    "watermark, text, signature"
)
EDIT_NEG_CLOTHING = (
    "bikini, swimsuit, swimwear, clothing, clothes, lingerie, bra, underwear, "
    "top, dress, shirt"
)
NUDE_HINT_WORDS = (
    "naked", "nude", "nudity", "topless", "bare skin", "bare breast", "bare chest",
    "undress", "strip", "unclothed", "bottomless", "take off", "clothes off",
    "without clothes", "no clothing", "remove her cloth", "remove their cloth",
    "remove the cloth", "remove his cloth",
    # exposure / explicit-anatomy intents (picnsfw leans permissive by design)
    "expose", "exposed", "tits", "tit ", "boobs", "boob ", "titties", "titty",
    "nipple", "areola", "breasts out", "breast out", "boobs out", "tits out",
    "cleavage out", "bra off", "no bra", "remove bra", "remove her bra",
    "show her breast", "show their breast", "show breast", "show her boob",
    "show her tit", "show tits", "show boobs", "flash her", "see through",
    "see-through", "sheer", "lingerie only", "panties only",
)

# Per-image edit analysis: the local Claude CLI reads the seed and returns tailored,
# strictly-SFW options (scene caption + artifact negatives + a strength
# recommendation). The analysis NEVER mentions or is told about undressing —
# the nudity terms are appended by code, not authored by Claude.
CLAUDE_ANALYZE_TIMEOUT = 45
ANALYZE_PROMPT = (
    "Analyze the image at %s for an image-to-image re-render. Output ONLY a JSON "
    "object (no markdown fences) with keys: "
    '"caption": one concise sentence, max 30 words — number of people and body '
    "type, their poses, the setting, the most distinctive background feature, and "
    "the lighting; do NOT mention clothing. "
    '"subjects": integer count of people. '
    '"negatives": array of short strings of artifacts to avoid so the scene does '
    'not multiply or distort (e.g. "crowd", "extra people", "deformed hands", plus '
    "anything scene-specific that must not duplicate). "
    '"strength": number 0.45-0.65, lower for a single clear subject, higher if '
    "more of the frame must change. Output only the JSON object."
)

# Provenance: ids of images the bot generated itself (synthetic subjects). The
# undress edit path is gated to these so it can never run on an arbitrary
# uploaded photo of a real person.
PROVENANCE_MAX = 1000


def _ip_is_safe(ip):
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in ipaddress.ip_network("100.64.0.0/10"):  # CGNAT
            return False
    return True


def _host_is_safe(host):
    """True only if every address `host` resolves to is a public IP (SSRF guard)."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            return False
        if not _ip_is_safe(ip):
            return False
    return True


class CreateError(Exception):
    pass


class GeminiRefusal(CreateError):
    """Gemini declined to generate (content filter / safety), as opposed to a
    transient or infrastructure error. Caller may retry with a softened prompt."""
    pass


class Create(callbacks.Plugin):
    """Image/video generation: !pic, !picnsfw, !video, !videonsfw."""

    threaded = True
    _prov_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def _runware_image(self, prompt, model, timeout, width=1024, height=1024,
                       reference_images=None, seed_image=None, strength=None,
                       negative_prompt=None):
        api_key = os.environ.get("RUNWARE_API_KEY")
        if not api_key:
            raise CreateError("RUNWARE_API_KEY not set")
        task = {
            "taskType": "imageInference",
            "taskUUID": str(uuid.uuid4()),
            "positivePrompt": prompt,
            "model": model,
            "width": width,
            "height": height,
            "numberResults": 1,
            "outputType": "URL",
        }
        if negative_prompt:
            task["negativePrompt"] = negative_prompt
        if reference_images:
            # instruction edit (FLUX Kontext): edits these image(s). Kontext
            # rejects steps/CFGScale (error unsupportedArchitectureCFGScale).
            task["referenceImages"] = reference_images
        else:
            # SDXL/Flux text-to-image, or img2img when seed_image is given (the
            # uncensored !picnsfw edit fallback); both take steps/CFGScale.
            task["steps"] = 25
            task["CFGScale"] = 7
            if seed_image:
                task["seedImage"] = seed_image
                if strength is not None:
                    task["strength"] = strength
        body = json.dumps([task]).encode("utf-8")
        req = urllib.request.Request(
            RUNWARE_ENDPOINT,
            data=body,
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if "insufficientCredits" in detail:
                raise CreateError("Runware credits exhausted — top up at my.runware.ai")
            raise CreateError("runware http %d: %s" % (e.code, detail[:200]))
        except Exception as e:
            raise CreateError("runware request failed: %s" % e)
        try:
            j = json.loads(data.decode("utf-8"))
        except Exception:
            raise CreateError("runware returned non-JSON: %s" % data[:200])
        errs = j.get("errors") or []
        if errs:
            code = errs[0].get("code") or ""
            if code == "insufficientCredits":
                raise CreateError("Runware credits exhausted — top up at my.runware.ai")
            msg_text = code or errs[0].get("message") or str(errs[0])
            raise CreateError("runware: " + msg_text[:200])
        results = j.get("data") or []
        if not results or "imageURL" not in results[0]:
            raise CreateError("runware response had no imageURL: %s" % json.dumps(j)[:200])
        return results[0]["imageURL"]

    def _gemini_image(self, prompt, timeout, image=None):
        """Generate (or edit) an image via Gemini (Nano Banana). Returns (raw_bytes, mime).

        If `image` is (bytes, mime), it is sent as an inline reference so Gemini
        edits it per the prompt. Raises GeminiRefusal on a content-filter refusal
        (Gemini is SFW-only) and CreateError on transient/infrastructure errors.
        """
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise CreateError("GEMINI_API_KEY not set")
        parts = [{"text": prompt}]
        if image is not None:
            raw_in, mime_in = image
            parts.append({"inline_data": {
                "mime_type": mime_in or "image/png",
                "data": base64.b64encode(raw_in).decode("ascii"),
            }})
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        req = urllib.request.Request(
            GEMINI_ENDPOINT + "?key=" + api_key,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                j = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise CreateError("gemini http %d: %s" % (e.code, detail))
        except Exception as e:
            raise CreateError("gemini request failed: %s" % e)
        cands = j.get("candidates") or []
        if not cands:
            fb = (j.get("promptFeedback") or {}).get("blockReason")
            raise GeminiRefusal("gemini: no image (%s)" % (fb or "blocked/empty"))
        parts = (cands[0].get("content") or {}).get("parts") or []
        for p in parts:
            d = p.get("inlineData") or p.get("inline_data")
            if d and d.get("data"):
                mime = d.get("mimeType") or d.get("mime_type") or "image/png"
                try:
                    return base64.b64decode(d["data"]), mime
                except Exception as e:
                    raise CreateError("gemini: bad image data (%s)" % e)
        fr = cands[0].get("finishReason") or ""
        # No image part: Gemini produced no image, which on this model means a
        # content refusal (IMAGE_SAFETY / NO_IMAGE / PROHIBITED_CONTENT / etc.).
        raise GeminiRefusal("gemini: no image part (finish=%s)" % (fr or "unknown"))

    def _zipline_upload(self, raw, mime, timeout):
        """Upload raw image bytes to Zipline. Returns the hosted URL."""
        token = os.environ.get("ZIPLINE_TOKEN")
        endpoint = os.environ.get("ZIPLINE_UPLOAD_URL")
        host = os.environ.get("ZIPLINE_HOST")
        if not token or not endpoint:
            raise CreateError("ZIPLINE_TOKEN/ZIPLINE_UPLOAD_URL not set")
        ext = self._ext_for_mime(mime)
        boundary = "----fatbot" + uuid.uuid4().hex
        head = (
            "--" + boundary + "\r\n"
            'Content-Disposition: form-data; name="file"; filename="%s.%s"\r\n'
            "Content-Type: %s\r\n\r\n" % (uuid.uuid4().hex, ext, mime or "image/png")
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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                j = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise CreateError("zipline http %d: %s" % (e.code, detail))
        except Exception as e:
            raise CreateError("zipline upload failed: %s" % e)
        files = j.get("files") or []
        if not files or "url" not in files[0]:
            raise CreateError("zipline: no url in response: %s" % json.dumps(j)[:200])
        url = files[0]["url"]
        # If a public base is configured, rebuild the URL against it (the
        # upload goes to the internal IP, so the returned host/scheme is
        # internal — swap in the public https host for IRC).
        base = os.environ.get("ZIPLINE_PUBLIC_BASE")
        if base:
            from urllib.parse import urlsplit
            url = base.rstrip("/") + urlsplit(url).path
        return url

    @staticmethod
    def _ext_for_mime(mime):
        m = (mime or "").lower()
        table = {
            "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
            "image/webp": "webp", "image/gif": "gif",
            "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
        }
        if m in table:
            return table[m]
        if "/" in m:
            sub = m.split("/", 1)[1]
            if sub.isalnum():
                return sub
        return "png"

    def _fetch_media(self, url, allow=("image/", "video/"), cap=MEDIA_MAX_BYTES):
        """Fetch a media URL -> (bytes, content_type). SSRF-guarded, size-capped,
        follows up to 5 redirects revalidating the host each hop. Used to pull a
        finished video off an external CDN so we can re-host it."""
        try:
            from curl_cffi import requests as cc
        except Exception as e:
            raise CreateError("media fetch unavailable: %s" % e)
        sess = cc.Session(impersonate=_IMPERSONATE)
        visited = set()
        cur = url
        for _ in range(_MAX_HOPS + 1):
            if cur in visited:
                raise CreateError("redirect loop")
            visited.add(cur)
            parsed = urllib.parse.urlsplit(cur)
            if parsed.scheme not in ("http", "https"):
                raise CreateError("only http(s) URLs are accepted")
            if not _host_is_safe(parsed.hostname or ""):
                raise CreateError("host resolves to a non-public address")
            try:
                r = sess.get(cur, timeout=30, allow_redirects=False, stream=True,
                             headers={"Accept": "*/*"})
            except Exception as e:
                raise CreateError("fetch failed: %s" % e)
            try:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location") or r.headers.get("location")
                    if not loc:
                        raise CreateError("redirect without Location")
                    cur = urllib.parse.urljoin(cur, loc)
                    continue
                if r.status_code != 200:
                    raise CreateError("HTTP %s" % r.status_code)
                ct = (r.headers.get("content-type")
                      or r.headers.get("Content-Type") or "")
                ct = ct.split(";", 1)[0].strip().lower()
                if allow and not any(ct.startswith(a) for a in allow):
                    raise CreateError("unexpected content-type (%s)" % (ct or "unknown"))
                buf = bytearray()
                for chunk in r.iter_content(chunk_size=262144):
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > cap:
                        raise CreateError("media exceeds %d bytes" % cap)
                if not buf:
                    raise CreateError("empty media")
                return bytes(buf), ct
            finally:
                try:
                    r.close()
                except Exception:
                    pass
        raise CreateError("too many redirects")

    def _rehost_image(self, url, timeout):
        """Re-host an external image (e.g. a Runware edit result) on Zipline so
        the posted link is a clean, short img.example.net URL (no t.ly underscore
        mangling). Returns the original URL on any failure."""
        try:
            data, ct = self._fetch_media(url, allow=("image/",), cap=EDIT_MAX_IMAGE_BYTES)
            return self._zipline_upload(data, ct, timeout)
        except Exception as e:
            self.log.warning("Create: image re-host failed (%s), posting original URL", e)
            return url

    def _rehost_video(self, video_url, timeout):
        """Re-host an external video on Zipline so it plays inline in a browser
        (Atlas/CDN URLs serve with a download disposition). Returns the Zipline
        URL, or the original URL on any failure — never breaks the command."""
        try:
            data, ct = self._fetch_media(video_url)
            if not ct.startswith("video/"):
                ct = "video/mp4"
            return self._zipline_upload(data, ct, timeout)
        except Exception as e:
            self.log.warning("Create: video re-host failed (%s), posting original URL", e)
            return video_url

    def _atlas_i2v(self, prompt, image_url, timeout):
        """Generate a video via Atlas I2V, then re-host it on Zipline for inline
        browser playback (falls back to the raw Atlas URL if re-hosting fails)."""
        url = self._atlas_i2v_raw(prompt, image_url, timeout)
        return self._rehost_video(url, timeout)

    def _atlas_i2v_raw(self, prompt, image_url, timeout):
        import time
        api_key = os.environ.get("ATLASCLOUD_API_KEY")
        if not api_key:
            raise CreateError("ATLASCLOUD_API_KEY not set")
        payload = {
            "model": ATLAS_I2V_MODEL,
            "prompt": prompt,
            "image": image_url,
            "duration": 5,
        }
        req = urllib.request.Request(
            ATLAS_ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": ATLAS_UA,
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
            raise CreateError("atlas http %d: %s" % (e.code, detail))
        except Exception as e:
            raise CreateError("atlas request failed: %s" % e)
        pred = j.get("data") or j
        pred_id = pred.get("id")
        if not pred_id:
            raise CreateError("atlas: no prediction id in response: %s" % json.dumps(j)[:200])
        deadline = time.time() + timeout
        poll_req_headers = {
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
            "User-Agent": ATLAS_UA,
        }
        while time.time() < deadline:
            time.sleep(5)
            preq = urllib.request.Request(
                ATLAS_POLL_BASE + pred_id, headers=poll_req_headers, method="GET",
            )
            try:
                with urllib.request.urlopen(preq, timeout=30) as resp:
                    pj = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                self.log.warning("Create: atlas poll error: %s", e)
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
                raise CreateError("atlas: completed but no output url: %s" % json.dumps(pj)[:200])
            if status in ("failed", "error", "cancelled"):
                err = d.get("error") or d.get("message") or json.dumps(pj)[:200]
                raise CreateError("atlas failed: %s" % err)
        raise CreateError("atlas: timeout after %ds" % timeout)

    def _shorten(self, url):
        # Our own Zipline URLs are already short and clean (no trailing
        # punctuation that IRC auto-linkers mangle) — never shorten them.
        base = os.environ.get("ZIPLINE_PUBLIC_BASE") or ""
        if base and url.startswith(base):
            return url
        try:
            for irc in world.ircs:
                shrink = irc.getCallback("ShrinkUrl")
                if shrink is not None:
                    try:
                        return shrink._getTlyUrl(url)
                    except Exception:
                        try:
                            return shrink._getTinyURL(url)
                        except Exception:
                            pass
        except Exception:
            pass
        return url

    def _clean_suffix(self, raw):
        suffix = (raw or "").strip().strip('"').strip("'").rstrip(".")
        if not suffix:
            return None
        if any(m in suffix.lower() for m in REFUSAL_MARKERS):
            return None
        return suffix

    def _expand_via_claude(self, prompt, system=EXPAND_SYSTEM):
        env = {
            "HOME": "/home/botuser",
            "PATH": "/home/botuser/.local/bin:/usr/bin:/bin",
            "CLAUDE_CONFIG_DIR": CLAUDE_CONFIG_DIR,
            "XDG_CACHE_HOME": "/home/botuser/runbot/.cache",
            "XDG_CONFIG_HOME": "/home/botuser/runbot/.config",
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        cmd = [
            CLAUDE_BIN,
            "-p",
            "--model", CLAUDE_MODEL,
            "--no-session-persistence",
            "--disable-slash-commands",
            "--system-prompt", system,
        ]
        try:
            result = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                timeout=CLAUDE_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired:
            self.log.warning("Create: claude expand timed out")
            return None
        except Exception as e:
            self.log.warning("Create: claude expand failed (%s)", e)
            return None
        if result.returncode != 0:
            self.log.warning(
                "Create: claude exit %d stderr=%r",
                result.returncode, (result.stderr or "")[:300],
            )
            return None
        return self._clean_suffix(result.stdout)

    def _expand_via_atlas(self, prompt, system=EXPAND_SYSTEM):
        api_key = os.environ.get("ATLASCLOUD_API_KEY")
        if not api_key:
            self.log.warning("Create: ATLASCLOUD_API_KEY not set, no fallback")
            return None
        body = {
            "model": ATLAS_LLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 400,
            "temperature": 1.0,
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            ATLAS_LLM_ENDPOINT,
            data=data,
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
                "User-Agent": ATLAS_UA,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=ATLAS_LLM_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                err_body = ""
            self.log.warning("Create: atlas-llm HTTP %d: %s", e.code, err_body)
            return None
        except Exception as e:
            self.log.warning("Create: atlas-llm request failed (%s)", e)
            return None
        try:
            choices = payload.get("choices") or []
            if not choices:
                self.log.info("Create: atlas-llm no choices in response")
                return None
            text = choices[0].get("message", {}).get("content", "") or ""
        except Exception:
            self.log.warning("Create: atlas-llm unexpected response shape")
            return None
        return self._clean_suffix(text)

    def _expand_prompt(self, prompt):
        suffix = self._expand_via_claude(prompt)
        source = "claude"
        if suffix is None:
            self.log.info("Create: claude refused/failed, trying atlas-grok")
            suffix = self._expand_via_atlas(prompt)
            source = "atlas-grok"
        if suffix is None:
            self.log.info("Create: both expanders refused/failed, using original")
            return prompt
        combined = prompt.rstrip(",. ") + ", " + suffix
        self.log.info("Create: expanded via %s: %r -> %r", source, prompt, combined)
        return combined

    def _rephrase_edit(self, instruction):
        """Turn a casual edit request into an explicit imperative instruction for
        the editor. Returns the rephrased instruction, or the original on failure."""
        out = self._expand_via_claude(instruction, system=EDIT_REPHRASE_SYSTEM)
        if out is None:
            out = self._expand_via_atlas(instruction, system=EDIT_REPHRASE_SYSTEM)
        return out or instruction

    # ------------------------------------------------------ image-edit helpers

    def _download_image(self, url):
        """Fetch an image URL -> (bytes, content_type). SSRF-guarded, size-capped,
        image/* only, follows up to 5 redirects revalidating the host each hop.
        Uses curl_cffi (Chrome impersonation) so CDNs like pbs.twimg.com and
        Cloudflare-fronted hosts serve us, same as the imageview MCP."""
        try:
            from curl_cffi import requests as cc
        except Exception as e:
            raise CreateError("image fetch unavailable: %s" % e)
        sess = cc.Session(impersonate=_IMPERSONATE)
        visited = set()
        cur = url
        for _ in range(_MAX_HOPS + 1):
            if cur in visited:
                raise CreateError("redirect loop")
            visited.add(cur)
            parsed = urllib.parse.urlsplit(cur)
            if parsed.scheme not in ("http", "https"):
                raise CreateError("only http(s) URLs are accepted")
            if not _host_is_safe(parsed.hostname or ""):
                raise CreateError("host resolves to a non-public address")
            try:
                r = sess.get(
                    cur, timeout=10, allow_redirects=False, stream=True,
                    headers={
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                        "Sec-Fetch-Dest": "image",
                        "Sec-Fetch-Mode": "no-cors",
                        "Sec-Fetch-Site": "cross-site",
                    },
                )
            except Exception as e:
                raise CreateError("fetch failed: %s" % e)
            try:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location") or r.headers.get("location")
                    if not loc:
                        raise CreateError("redirect without Location")
                    cur = urllib.parse.urljoin(cur, loc)
                    continue
                if r.status_code != 200:
                    raise CreateError("HTTP %s" % r.status_code)
                ct = (r.headers.get("content-type")
                      or r.headers.get("Content-Type") or "")
                ct = ct.split(";", 1)[0].strip().lower()
                if not ct.startswith("image/"):
                    raise CreateError("not an image (%s)" % (ct or "unknown"))
                buf = bytearray()
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > EDIT_MAX_IMAGE_BYTES:
                        raise CreateError(
                            "image exceeds %d bytes" % EDIT_MAX_IMAGE_BYTES)
                if not buf:
                    raise CreateError("empty image")
                return bytes(buf), ct
            finally:
                try:
                    r.close()
                except Exception:
                    pass
        raise CreateError("too many redirects")

    @staticmethod
    def _image_dims(data):
        """Best-effort (width, height) for PNG/GIF/WEBP/JPEG, else None."""
        try:
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                w = int.from_bytes(data[16:20], "big")
                h = int.from_bytes(data[20:24], "big")
                return (w, h) if w and h else None
            if data[:6] in (b"GIF87a", b"GIF89a"):
                w = int.from_bytes(data[6:8], "little")
                h = int.from_bytes(data[8:10], "little")
                return (w, h) if w and h else None
            if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                fmt = data[12:16]
                if fmt == b"VP8 ":
                    w = int.from_bytes(data[26:28], "little") & 0x3FFF
                    h = int.from_bytes(data[28:30], "little") & 0x3FFF
                    return (w, h) if w and h else None
                if fmt == b"VP8L" and data[20] == 0x2F:
                    bits = int.from_bytes(data[21:25], "little")
                    w = (bits & 0x3FFF) + 1
                    h = ((bits >> 14) & 0x3FFF) + 1
                    return (w, h) if w and h else None
                if fmt == b"VP8X":
                    w = int.from_bytes(data[24:27], "little") + 1
                    h = int.from_bytes(data[27:30], "little") + 1
                    return (w, h) if w and h else None
            if data[:2] == b"\xff\xd8":
                i, n = 2, len(data)
                while i + 9 < n:
                    if data[i] != 0xFF:
                        i += 1
                        continue
                    marker = data[i + 1]
                    if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                        i += 2
                        continue
                    seglen = int.from_bytes(data[i + 2:i + 4], "big")
                    if seglen < 2:
                        break
                    if marker in _JPEG_SOF:
                        h = int.from_bytes(data[i + 5:i + 7], "big")
                        w = int.from_bytes(data[i + 7:i + 9], "big")
                        return (w, h) if w and h else None
                    i += 2 + seglen
        except Exception:
            pass
        return None

    @staticmethod
    def _kontext_dims(w, h):
        """Pick the supported FLUX Kontext size whose aspect ratio is closest to
        the source image (Kontext rejects anything off its fixed list)."""
        if w <= 0 or h <= 0:
            return 1024, 1024
        ar = w / h

        def closeness(d):
            r = ar / (d[0] / d[1])
            return max(r, 1.0 / r)  # symmetric ratio distance, >= 1

        return min(KONTEXT_DIMS, key=closeness)

    @staticmethod
    def _target_dims(w, h):
        """SDXL-friendly size (~1MP, /64-aligned) preserving aspect ratio, for
        the uncensored img2img fallback."""
        if w <= 0 or h <= 0:
            return 1024, 1024
        long_side = 1024
        if w >= h:
            nw, nh = long_side, long_side * h / w
        else:
            nw, nh = long_side * w / h, long_side

        def snap(v):
            return max(512, min(1536, int(round(v / 64.0)) * 64))

        return snap(nw), snap(nh)

    @staticmethod
    def _is_moderation_error(err):
        """True if a Runware error looks like provider NSFW moderation."""
        s = str(err).lower()
        return any(m in s for m in (
            "invalidprovidercontent", "invalidbflcontent",
            "moderation", "flagged", "invalid content"))

    def _caption_image(self, raw, mime, timeout):
        """Caption a seed image via Gemini vision — a concise SFW scene
        description (no clothing) used to anchor an img2img edit so it doesn't
        drift the composition/background. Returns the caption, or None."""
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return None
        body = {"contents": [{"parts": [
            {"text": CAPTION_PROMPT},
            {"inline_data": {"mime_type": mime or "image/png",
                             "data": base64.b64encode(raw).decode("ascii")}},
        ]}]}
        req = urllib.request.Request(
            GEMINI_CAPTION_ENDPOINT + "?key=" + api_key,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                j = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self.log.warning("Create: caption failed (%s)", e)
            return None
        try:
            cands = j.get("candidates") or []
            if not cands:
                return None
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            text = text.strip().strip('"').replace("\n", " ").strip()
            return text or None
        except Exception:
            return None

    @staticmethod
    def _is_undress(instruction):
        s = (instruction or "").lower()
        return any(w in s for w in NUDE_HINT_WORDS)

    # ----------------------------------------------------- provenance (gate)

    def _provenance_path(self):
        try:
            base = conf.supybot.directories.data()
        except Exception:
            base = "/home/botuser/runbot/data"
        return os.path.join(base, "Create_provenance.json")

    def _zid_from_url(self, url):
        """Zipline file id from one of OUR public image URLs, else None."""
        base = os.environ.get("ZIPLINE_PUBLIC_BASE") or ""
        if not base or not url or not url.startswith(base):
            return None
        seg = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        return (seg.split(".", 1)[0] or None) if seg else None

    def _record_provenance(self, url, prompt, kind):
        """Mark an image WE generated as synthetic (keyed by its Zipline id)."""
        import time
        zid = self._zid_from_url(url)
        if not zid:
            return
        path = self._provenance_path()
        with self._prov_lock:
            try:
                with open(path) as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            data[zid] = {"prompt": (prompt or "")[:500], "kind": kind, "ts": int(time.time())}
            if len(data) > PROVENANCE_MAX:
                newest = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0),
                                reverse=True)[:PROVENANCE_MAX]
                data = dict(newest)
            try:
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, path)
            except Exception as e:
                self.log.warning("Create: provenance write failed (%s)", e)

    def _seed_is_synthetic(self, url):
        """True only if `url` is an image this bot generated (gate for undress)."""
        zid = self._zid_from_url(url)
        if not zid:
            return False
        with self._prov_lock:
            try:
                with open(self._provenance_path()) as f:
                    data = json.load(f)
            except Exception:
                return False
        return isinstance(data, dict) and zid in data

    # ------------------------------------------------ per-image edit analysis

    @staticmethod
    def _parse_analysis(text):
        if not text:
            return None
        s = text.strip()
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            obj = json.loads(s[i:j + 1])
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    def _analyze_via_claude(self, image_path):
        """the local Claude CLI reads the seed and returns SFW edit options (or None)."""
        env = {
            "HOME": "/home/botuser",
            "PATH": "/home/botuser/.local/bin:/usr/bin:/bin",
            "CLAUDE_CONFIG_DIR": CLAUDE_CONFIG_DIR,
            "XDG_CACHE_HOME": "/home/botuser/runbot/.cache",
            "XDG_CONFIG_HOME": "/home/botuser/runbot/.config",
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        cmd = [
            CLAUDE_BIN, "-p", "--model", CLAUDE_MODEL,
            "--no-session-persistence", "--disable-slash-commands",
            "--allowedTools", "Read",
        ]
        try:
            result = subprocess.run(
                cmd, input=ANALYZE_PROMPT % image_path, capture_output=True,
                text=True, timeout=CLAUDE_ANALYZE_TIMEOUT, env=env,
            )
        except Exception as e:
            self.log.warning("Create: claude analyze failed (%s)", e)
            return None
        if result.returncode != 0:
            self.log.warning("Create: claude analyze exit %d: %s",
                             result.returncode, (result.stderr or "")[:200])
            return None
        return self._parse_analysis(result.stdout)

    def _analyze_seed(self, raw, mime, timeout):
        """Tailored SFW edit options for the seed: the local Claude CLI (vision) first,
        Gemini caption as fallback, else None. The undress instruction is NEVER
        sent here — this step only describes the scene."""
        ext = self._ext_for_mime(mime)
        path = None
        try:
            fd, path = tempfile.mkstemp(prefix="seed_", suffix="." + ext, dir="/tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            analysis = self._analyze_via_claude(path)
            if analysis and analysis.get("caption"):
                return analysis
        except Exception as e:
            self.log.warning("Create: analyze_seed error (%s)", e)
        finally:
            if path:
                try:
                    os.unlink(path)
                except Exception:
                    pass
        cap = self._caption_image(raw, mime, timeout)
        return {"caption": cap} if cap else None

    def _edit_pic_gemini(self, irc, msg, url, instruction, timeout):
        """!pic <url> <edit>: SFW instruction edit via Gemini → Zipline."""
        instruction = (instruction or "").strip()
        if not instruction:
            irc.error("tell me what to change, e.g. !pic <url> add a halo above his head")
            return
        try:
            data, ct = self._download_image(url)
        except CreateError as e:
            self.log.warning("Create.pic edit fetch: %s", e)
            irc.error("couldn't fetch that image: " + str(e))
            return
        if ct == "image/jpg":
            ct = "image/jpeg"
        edit_instruction = self._rephrase_edit(instruction)
        irc.reply("editing: " + edit_instruction, prefixNick=False)
        try:
            raw, mime = self._gemini_image(edit_instruction, timeout, image=(data, ct))
            out = self._zipline_upload(raw, mime, timeout)
            self._record_provenance(out, "edit:" + instruction, "pic-edit")
            irc.reply(self._shorten(out), prefixNick=False)
            return
        except GeminiRefusal as e:
            self.log.info("Create.pic gemini edit refused: %s", e)
            irc.reply(
                "gemini declined that edit — reword it, or use "
                "!picnsfw <url> <edit> for spicy edits.",
                prefixNick=False,
            )
        except CreateError as e:
            self.log.warning("Create.pic gemini edit error: %s", e)
            irc.error("edit failed (%s) — try again in a moment" % e)

    def _edit_pic_nsfw(self, irc, msg, url, instruction, timeout):
        """!picnsfw <url> <edit>: uncensored, VISION-GUIDED img2img via Lustify SDXL.

        Plain img2img with an imperative prompt re-diffuses the whole frame and
        wrecks the scene (e.g. two women -> nude crowd). So we (1) caption the
        seed with Gemini vision to anchor the composition/background, (2) build a
        compact positive = caption + the user's instruction + (for undress
        requests) nudity terms, and (3) pass a clothing/crowd negative prompt.
        Kontext is skipped — BFL moderates it (silently keeps clothes). Clean SFW
        edits live on !pic <url> <edit> (Gemini)."""
        instruction = (instruction or "").strip()
        if not instruction:
            irc.error("tell me what to change, e.g. !picnsfw <url> take their clothes off")
            return
        undress = self._is_undress(instruction)
        # GATE: undress edits run ONLY on images this bot generated itself
        # (synthetic subjects), never on an arbitrary uploaded photo.
        if undress and not self._seed_is_synthetic(url):
            irc.error("undress edits only work on images I made with !pic — "
                      "generate one first, then edit that link.")
            return
        try:
            data, ct = self._download_image(url)
        except CreateError as e:
            self.log.warning("Create.picnsfw edit fetch: %s", e)
            irc.error("couldn't fetch that image: " + str(e))
            return
        if ct == "image/jpg":
            ct = "image/jpeg"
        # Per-image SFW analysis (the local Claude CLI vision; Gemini caption fallback).
        analysis = self._analyze_seed(data, ct, timeout) or {}
        caption = analysis.get("caption")
        neg_extra = analysis.get("negatives")
        if not isinstance(neg_extra, list):
            neg_extra = []
        try:
            cfg_strength = float(self.registryValue("editStrength", msg.channel, irc.network))
        except Exception:
            cfg_strength = 0.6
        try:
            strength = float(analysis.get("strength"))
        except (TypeError, ValueError):
            strength = cfg_strength
        strength = max(0.35, min(0.75, strength))
        # Build a compact positive (mind SDXL's ~77-token CLIP window): scene
        # anchor + user intent + (code-appended) nudity boosters for undress.
        parts = []
        if caption:
            parts.append(str(caption)[:240])
        parts.append(instruction)
        if undress:
            parts.append("completely nude, fully naked, bare skin, natural skin")
        parts.append("photorealistic, detailed, same composition and framing")
        positive = ", ".join(parts)
        neg_terms = [EDIT_NEG_BASE] + [str(n) for n in neg_extra if n]
        if undress:
            neg_terms.append(EDIT_NEG_CLOTHING)
        negative = ", ".join(neg_terms)
        irc.reply("editing (vision): " + (caption or instruction), prefixNick=False)
        dims = self._image_dims(data)
        ref_uri = "data:%s;base64,%s" % (ct, base64.b64encode(data).decode("ascii"))
        # Uncensored Lustify SDXL img2img (the only path that actually does NSFW).
        nmodel = self.registryValue("editFallbackModel", msg.channel, irc.network)
        sw, sh = self._target_dims(*dims) if dims else (1024, 1024)
        try:
            out = self._runware_image(positive, nmodel, timeout,
                                      width=sw, height=sh,
                                      seed_image=ref_uri, strength=strength,
                                      negative_prompt=negative)
        except CreateError as e:
            self.log.warning("Create.picnsfw edit img2img: %s", e)
            irc.error(str(e))
            return
        out = self._rehost_image(out, timeout)
        # The result is itself a bot-generated synthetic image — allow chaining.
        self._record_provenance(out, "edit:" + instruction, "picnsfw-edit")
        irc.reply(NSFW_PREFIX + " " + self._shorten(out), prefixNick=False)

    def _check_cap(self, irc, msg, cap_name):
        if not msg.channel:
            irc.error("channel-only")
            return False
        chan_cap = ircdb.makeChannelCapability(msg.channel, cap_name)
        if not ircdb.checkCapability(msg.prefix, chan_cap):
            irc.errorNoCapability(chan_cap)
            return False
        return True

    def _parse_prompt(self, irc, prompt, cmd):
        prompt = prompt.strip()
        if not prompt:
            irc.error("usage: !%s <prompt>" % cmd)
            return None
        if len(prompt) > 4000:
            irc.error("prompt too long")
            return None
        return prompt

    def _display_prompt(self, irc, full_prompt, original):
        """Display prompt in IRC, truncating if needed with [...]"""
        if full_prompt == original:
            return
        IRC_MAX = 400
        if len(full_prompt) <= IRC_MAX:
            irc.reply("prompt: " + full_prompt, prefixNick=False)
        else:
            truncated = full_prompt[:IRC_MAX-5] + "[...]"
            irc.reply("prompt: " + truncated, prefixNick=False)

    def _extract_seed(self, prompt, default=DEFAULT_MOTION_PROMPT):
        """Return (seed_url, rest_text) if prompt contains a URL, else (None, prompt).

        The URL is removed from the rest text; an empty rest is replaced with
        `default` (a generic motion phrase for video; pass "" for !pic edits)."""
        m = SEED_URL_RE.search(prompt or "")
        if not m:
            return None, prompt
        url = m.group(0).rstrip(".,;:!?)>]'\"")
        rest = (prompt[:m.start()] + prompt[m.end():]).strip(" ,.;:-")
        if not rest:
            rest = default
        return url, rest

    def _gemini_to_zipline(self, prompt, timeout):
        """Gemini image → Zipline upload. Returns hosted URL or raises."""
        raw, mime = self._gemini_image(prompt, timeout)
        return self._zipline_upload(raw, mime, timeout)

    # ------------------------------------------------------------------ !pic

    def pic(self, irc, msg, args, prompt):
        """<prompt> | <image-url> <edit> — generate an SFW image via Gemini
        (Nano Banana, hosted on Zipline), or edit a linked image via Gemini.

        Gemini is SFW-only. On a content refusal it retries once with a
        toned-down prompt; if that is also refused it asks you to reword.
        For NSFW use !picnsfw (Flux/Kontext).
        """
        if not self._check_cap(irc, msg, "generative"):
            return
        prompt = self._parse_prompt(irc, prompt, "pic")
        if prompt is None:
            return
        timeout = self.registryValue("timeoutSec")
        # Edit path: a linked image + an instruction.
        seed_url, instruction = self._extract_seed(prompt, default="")
        if seed_url:
            self._edit_pic_gemini(irc, msg, seed_url, instruction, timeout)
            return
        # Text-to-image path.
        original = prompt
        prompt = self._expand_prompt(original)
        self._display_prompt(irc, prompt, original)
        # Attempt 1: Gemini → Zipline.
        try:
            url = self._gemini_to_zipline(prompt, timeout)
            self._record_provenance(url, original, "pic")
            irc.reply(self._shorten(url), prefixNick=False)
            return
        except GeminiRefusal as e:
            self.log.info("Create.pic gemini refused: %s — retrying toned-down", e)
        except CreateError as e:
            # Transient / infrastructure error (HTTP 5xx, Zipline down, etc.).
            self.log.warning("Create.pic gemini/zipline error: %s", e)
            irc.error("image generation failed (%s) — try again in a moment" % e)
            return
        # Attempt 2: re-expand the original with an SFW nudge and retry.
        irc.reply("gemini declined that — retrying with a tamer take…", prefixNick=False)
        retry_prompt = self._expand_prompt(original + SFW_RETRY_HINT)
        self._display_prompt(irc, retry_prompt, original)
        try:
            url = self._gemini_to_zipline(retry_prompt, timeout)
            self._record_provenance(url, original, "pic")
            irc.reply(self._shorten(url), prefixNick=False)
            return
        except GeminiRefusal as e:
            self.log.info("Create.pic gemini refused on retry: %s", e)
            irc.reply(
                "gemini won't generate this prompt (declined twice). "
                "try again or reword it a bit — or use !picnsfw for spicy stuff.",
                prefixNick=False,
            )
        except CreateError as e:
            self.log.warning("Create.pic gemini/zipline error on retry: %s", e)
            irc.error("image generation failed (%s) — try again in a moment" % e)

    pic = wrap(pic, ["public", "text"])

    # -------------------------------------------------------------- !picnsfw

    def picnsfw(self, irc, msg, args, prompt):
        """<prompt> | <image-url> <edit> — generate an image via Flux (NSFW-capable),
        or edit a linked image (FLUX Kontext, uncensored img2img fallback)."""
        if not self._check_cap(irc, msg, "generative"):
            return
        prompt = self._parse_prompt(irc, prompt, "picnsfw")
        if prompt is None:
            return
        timeout = self.registryValue("timeoutSec")
        # Edit path: a linked image + an instruction.
        seed_url, instruction = self._extract_seed(prompt, default="")
        if seed_url:
            self._edit_pic_nsfw(irc, msg, seed_url, instruction, timeout)
            return
        # Text-to-image path.
        model = self.registryValue("model", msg.channel, irc.network)
        original = prompt
        prompt = self._expand_prompt(prompt)
        self._display_prompt(irc, prompt, original)
        try:
            url = self._runware_image(prompt, model, timeout)
        except CreateError as e:
            self.log.warning("Create.picnsfw: %s", e)
            irc.error(str(e))
            return
        irc.reply(NSFW_PREFIX + " " + self._shorten(url), prefixNick=False)

    picnsfw = wrap(picnsfw, ["public", "text"])

    # ------------------------------------------------------------------ !video

    def _resolve_url(self, url):
        """Follow redirects (e.g. is.gd shortlinks) and return the final URL.

        Best-effort: returns the input URL on any failure."""
        try:
            req = urllib.request.Request(
                url, method="HEAD",
                headers={"User-Agent": ATLAS_UA, "Accept": "*/*"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.geturl()
        except Exception:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": ATLAS_UA, "Range": "bytes=0-0"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    final = resp.geturl()
                    return final
            except Exception:
                return url

    def video(self, irc, msg, args, prompt):
        """<prompt> — Flux Pro image → Atlas Wan 2.2 Turbo I2V.

        If <prompt> contains a URL, that URL is used as the seed image
        and the remaining text is treated as the motion description; the
        image step is skipped.
        """
        if not self._check_cap(irc, msg, "generative"):
            return
        prompt = self._parse_prompt(irc, prompt, "video")
        if prompt is None:
            return
        video_timeout = self.registryValue("videoTimeoutSec")
        seed_url, motion = self._extract_seed(prompt)
        if seed_url:
            image_url = self._resolve_url(seed_url)
            irc.reply("seed: " + self._shorten(image_url), prefixNick=False)
            try:
                video_url = self._atlas_i2v(motion, image_url, video_timeout)
            except CreateError as e:
                self.log.warning("Create.video video: %s", e)
                irc.error("video step: " + str(e))
                return
            irc.reply(self._shorten(video_url), prefixNick=False)
            return
        image_model = self.registryValue("picModel", msg.channel, irc.network)
        image_timeout = self.registryValue("timeoutSec")
        original = prompt
        prompt = self._expand_prompt(prompt)
        self._display_prompt(irc, prompt, original)
        try:
            image_url = self._runware_image(prompt, image_model, image_timeout)
        except CreateError as e:
            self.log.warning("Create.video image: %s", e)
            irc.error("image step: " + str(e))
            return
        irc.reply("seed: " + self._shorten(image_url), prefixNick=False)
        try:
            video_url = self._atlas_i2v(prompt, image_url, video_timeout)
        except CreateError as e:
            self.log.warning("Create.video video: %s", e)
            irc.error("video step: " + str(e))
            return
        irc.reply(self._shorten(video_url), prefixNick=False)

    video = wrap(video, ["public", "text"])

    # -------------------------------------------------------------- !videonsfw

    def videonsfw(self, irc, msg, args, prompt):
        """<prompt> — Flux Pro image → Atlas Cloud Wan 2.2 Turbo Spicy I2V.

        If <prompt> contains a URL, that URL is used as the seed image
        and the remaining text is treated as the motion description; the
        image step is skipped.
        """
        if not self._check_cap(irc, msg, "generative"):
            return
        prompt = self._parse_prompt(irc, prompt, "videonsfw")
        if prompt is None:
            return
        video_timeout = self.registryValue("videoTimeoutSec")
        seed_url, motion = self._extract_seed(prompt)
        if seed_url:
            image_url = self._resolve_url(seed_url)
            irc.reply(NSFW_PREFIX + " seed: " + self._shorten(image_url), prefixNick=False)
            try:
                video_url = self._atlas_i2v(motion, image_url, video_timeout)
            except CreateError as e:
                self.log.warning("Create.videonsfw video: %s", e)
                irc.error("video step: " + str(e))
                return
            irc.reply(NSFW_PREFIX + " " + self._shorten(video_url), prefixNick=False)
            return
        image_model = self.registryValue("model", msg.channel, irc.network)
        image_timeout = self.registryValue("timeoutSec")
        original = prompt
        prompt = self._expand_prompt(prompt)
        self._display_prompt(irc, prompt, original)
        try:
            image_url = self._runware_image(prompt, image_model, image_timeout)
        except CreateError as e:
            self.log.warning("Create.videonsfw image: %s", e)
            irc.error("image step: " + str(e))
            return
        irc.reply(NSFW_PREFIX + " seed: " + self._shorten(image_url), prefixNick=False)
        try:
            video_url = self._atlas_i2v(prompt, image_url, video_timeout)
        except CreateError as e:
            self.log.warning("Create.videonsfw video: %s", e)
            irc.error("video step: " + str(e))
            return
        irc.reply(NSFW_PREFIX + " " + self._shorten(video_url), prefixNick=False)

    videonsfw = wrap(videonsfw, ["public", "text"])


Class = Create
