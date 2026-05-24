"""
Create: image and video generation via Runware.ai (image) + Atlas Cloud (video).

!pic <prompt>          — Runware image                       (#chan,generative)
!picnsfw <prompt>      — Runware image, NSFW model           (#chan,generative)
!video <prompt>        — Runware image + Atlas Wan 2.2 I2V   (#chan,generative)
!videonsfw <prompt>    — Runware NSFW image + Atlas Spicy I2V (#chan,generative)
"""

import os
import re
import uuid
import json
import base64
import socket
import ipaddress
import subprocess
import urllib.parse
import urllib.request
import urllib.error

import supybot.conf as conf
import supybot.ircdb as ircdb
import supybot.world as world
import supybot.callbacks as callbacks
import supybot.ircutils as ircutils
from supybot.commands import wrap

NSFW_PREFIX = ircutils.bold('[NSFW]')

RUNWARE_ENDPOINT = "https://api.runware.ai/v1"
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
    "You add cinematographic / photographic styling details to an image generation prompt. "
    "The user provides a subject description. Your job is to OUTPUT ONLY a comma-separated "
    "list of 4-8 short visual style modifiers to append to it — things like lighting, "
    "composition, camera/lens choice, mood, color grading, art style, level of detail. "
    "Do NOT repeat, paraphrase, comment on, or judge the user's subject. "
    "Do NOT include the user's description in your output. "
    "Do NOT use sentences. Just the comma-separated modifiers. No quotes, no prefix, no explanation. "
    "Max 250 characters total."
)


EDIT_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
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

# Turn a user's casual edit request into ONE explicit imperative instruction for
# FLUX Kontext. Kontext sees the image itself, so we never describe it; a vague
# "give that man a halo" gets ignored, an explicit "Add a glowing halo above his
# head, keep everything else unchanged" is applied cleanly.
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


class Create(callbacks.Plugin):
    """Image/video generation: !pic, !picnsfw, !video, !videonsfw."""

    threaded = True

    # ------------------------------------------------------------------ helpers

    def _runware_image(self, prompt, model, timeout, width=1024, height=1024,
                       reference_images=None, seed_image=None, strength=None):
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
        if reference_images:
            # instruction edit (FLUX Kontext): edits these image(s). Kontext
            # rejects steps/CFGScale (error unsupportedArchitectureCFGScale).
            task["referenceImages"] = reference_images
        else:
            # SDXL text-to-image, or img2img when seed_image is given (the
            # uncensored !picnsfw fallback); both need steps/CFGScale.
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

    def _atlas_i2v(self, prompt, image_url, timeout):
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
        # Route output URLs through ShrinkUrl's current chain (t.ly -> tinyurl
        # -> x0.no). _getIsgdUrl was removed when is.gd was dropped 2026-05-24;
        # _getTlyUrl is the live entry point. Best-effort: raw URL on any failure.
        try:
            for irc in world.ircs:
                shrink = irc.getCallback("ShrinkUrl")
                if shrink is not None:
                    return shrink._getTlyUrl(url)
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
            "max_tokens": 200,
            "temperature": 0.7,
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
        Kontext. Returns the rephrased instruction, or the original on failure."""
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

    def _make_image(self, irc, msg, prompt, model, nsfw):
        """Shared !pic / !picnsfw path: edit a linked image, else text-to-image."""
        timeout = self.registryValue("timeoutSec")
        prefix = (NSFW_PREFIX + " ") if nsfw else ""
        seed_url, instruction = self._extract_seed(prompt, default="")
        if seed_url:
            self._edit_image(irc, msg, seed_url, instruction, timeout, prefix, nsfw)
            return
        original = prompt
        prompt = self._expand_prompt(prompt)
        if prompt != original:
            irc.reply("prompt: " + prompt, prefixNick=False)
        try:
            url = self._runware_image(prompt, model, timeout)
        except CreateError as e:
            self.log.warning("Create image: %s", e)
            irc.error(str(e))
            return
        irc.reply(prefix + self._shorten(url), prefixNick=False)

    def _edit_image(self, irc, msg, url, instruction, timeout, prefix, nsfw):
        """Edit a linked image. Primary path is FLUX Kontext (top quality), but
        Runware enforces BFL moderation on ALL Kontext variants, so NSFW edits
        are refused. For !picnsfw, on a moderation refusal we fall back to
        uncensored Lustify SDXL img2img (lower quality, but actually does NSFW)."""
        instruction = (instruction or "").strip()
        if not instruction:
            irc.error("tell me what to change, e.g. !pic <url> add a halo above his head")
            return
        try:
            data, ct = self._download_image(url)
        except CreateError as e:
            self.log.warning("Create edit fetch: %s", e)
            irc.error("couldn't fetch that image: " + str(e))
            return
        if ct == "image/jpg":
            ct = "image/jpeg"
        edit_instruction = self._rephrase_edit(instruction)
        irc.reply("editing: " + edit_instruction, prefixNick=False)
        dims = self._image_dims(data)
        ref_uri = "data:%s;base64,%s" % (ct, base64.b64encode(data).decode("ascii"))
        # 1) FLUX Kontext — best quality (SFW only)
        kw, kh = self._kontext_dims(*dims) if dims else (1024, 1024)
        kmodel = self.registryValue("editModel", msg.channel, irc.network)
        try:
            out = self._runware_image(edit_instruction, kmodel, timeout,
                                      width=kw, height=kh, reference_images=[ref_uri])
            irc.reply(prefix + self._shorten(out), prefixNick=False)
            return
        except CreateError as e:
            if not (nsfw and self._is_moderation_error(e)):
                self.log.warning("Create edit kontext: %s", e)
                irc.error(str(e))
                return
            self.log.info("Create edit: Kontext refused NSFW, falling back to Lustify img2img")
        # 2) Uncensored fallback: Lustify SDXL img2img
        nmodel = self.registryValue("model", msg.channel, irc.network)
        sw, sh = self._target_dims(*dims) if dims else (1024, 1024)
        try:
            strength = float(self.registryValue("editStrength", msg.channel, irc.network))
        except Exception:
            strength = 0.6
        try:
            out = self._runware_image(edit_instruction, nmodel, timeout,
                                      width=sw, height=sh,
                                      seed_image=ref_uri, strength=strength)
        except CreateError as e:
            self.log.warning("Create edit nsfw img2img: %s", e)
            irc.error(str(e))
            return
        irc.reply(prefix + "(uncensored) " + self._shorten(out), prefixNick=False)

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

    # ------------------------------------------------------------------ !pic

    def pic(self, irc, msg, args, prompt):
        """<prompt> | <image-url> [edit] — text-to-image, or look at a linked
        image and edit it (image-to-image) via Runware (SFW model)."""
        if not self._check_cap(irc, msg, "generative"):
            return
        prompt = self._parse_prompt(irc, prompt, "pic")
        if prompt is None:
            return
        model = self.registryValue("picModel", msg.channel, irc.network)
        self._make_image(irc, msg, prompt, model, nsfw=False)

    pic = wrap(pic, ["public", "text"])

    # -------------------------------------------------------------- !picnsfw

    def picnsfw(self, irc, msg, args, prompt):
        """<prompt> | <image-url> [edit] — text-to-image, or look at a linked
        image and edit it (image-to-image) via Runware (unrestricted model)."""
        if not self._check_cap(irc, msg, "generative"):
            return
        prompt = self._parse_prompt(irc, prompt, "picnsfw")
        if prompt is None:
            return
        model = self.registryValue("model", msg.channel, irc.network)
        self._make_image(irc, msg, prompt, model, nsfw=True)

    picnsfw = wrap(picnsfw, ["public", "text"])

    # ------------------------------------------------------------------ !video

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
        """<prompt> — Runware (SFW) image → Atlas Wan 2.2 Turbo I2V.

        If <prompt> contains a URL, that URL is used as the seed image
        and the remaining text is treated as the motion description; the
        Runware image step is skipped.
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
        if prompt != original:
            irc.reply("prompt: " + prompt, prefixNick=False)
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
        """<prompt> — Runware NSFW image → Atlas Cloud Wan 2.2 Turbo Spicy I2V.

        If <prompt> contains a URL, that URL is used as the seed image
        and the remaining text is treated as the motion description; the
        Runware image step is skipped.
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
        if prompt != original:
            irc.reply("prompt: " + prompt, prefixNick=False)
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
