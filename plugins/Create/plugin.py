"""
Create: image and video generation via Runware.ai (image) + Atlas Cloud (video).

!pic <prompt>          — Runware image                       (#chan,generative)
!picnsfw <prompt>      — Runware image, NSFW model           (#chan,generative)
!video <prompt>        — Runware image + Atlas Wan 2.2 I2V   (#chan,generative)
!videonsfw <prompt>    — Runware NSFW image + Atlas Spicy I2V (#chan,generative)
"""

import os
import uuid
import json
import subprocess
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
ATLAS_ENDPOINT = "https://api.atlascloud.ai/api/v1/model/generateVideo"
ATLAS_POLL_BASE = "https://api.atlascloud.ai/api/v1/model/prediction/"
ATLAS_I2V_MODEL = "atlascloud/wan-2.2-turbo-spicy/image-to-video"
ATLAS_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CLAUDE_BIN = "/home/botuser/.local/bin/claude"
CLAUDE_CONFIG_DIR = "/home/botuser/runbot/.claude"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_TIMEOUT = 30

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


class CreateError(Exception):
    pass


class Create(callbacks.Plugin):
    """Image/video generation: !pic, !picnsfw, !video, !videonsfw."""

    threaded = True

    # ------------------------------------------------------------------ helpers

    def _runware_image(self, prompt, model, timeout):
        api_key = os.environ.get("RUNWARE_API_KEY")
        if not api_key:
            raise CreateError("RUNWARE_API_KEY not set")
        task = {
            "taskType": "imageInference",
            "taskUUID": str(uuid.uuid4()),
            "positivePrompt": prompt,
            "model": model,
            "width": 1024,
            "height": 1024,
            "numberResults": 1,
            "steps": 25,
            "CFGScale": 7,
            "outputType": "URL",
        }
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
        try:
            for irc in world.ircs:
                shrink = irc.getCallback("ShrinkUrl")
                if shrink is not None:
                    return shrink._getIsgdUrl(url)
        except Exception:
            pass
        return url

    def _expand_prompt(self, prompt):
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
            "--system-prompt", EXPAND_SYSTEM,
        ]
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT,
                env=env,
            )
        except subprocess.TimeoutExpired:
            self.log.warning("Create: prompt expand timed out, using original")
            return prompt
        except Exception as e:
            self.log.warning("Create: prompt expand failed (%s), using original", e)
            return prompt
        if result.returncode != 0:
            self.log.warning(
                "Create: claude exit %d stderr=%r (using original)",
                result.returncode, (result.stderr or "")[:300],
            )
            return prompt
        suffix = (result.stdout or "").strip().strip('"').strip("'").rstrip(".")
        if not suffix:
            return prompt
        low = suffix.lower()
        refusal_markers = (
            "i'm not able", "i am not able", "i'm unable", "i am unable",
            "i can't", "i cannot", "i can not", "i won't", "i will not",
            "i'd be happy", "i would be happy", "i don't", "i do not",
            "sorry,", "sorry —", "sorry, but", "let me know", "happy to",
        )
        if any(m in low for m in refusal_markers):
            self.log.info("Create: expander refused (%r), using original prompt", suffix[:120])
            return prompt
        combined = prompt.rstrip(",. ") + ", " + suffix
        self.log.info("Create: expanded prompt: %r -> %r", prompt, combined)
        return combined

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
        """<prompt> — generate an image via Runware (SFW model)."""
        if not self._check_cap(irc, msg, "generative"):
            return
        prompt = self._parse_prompt(irc, prompt, "pic")
        if prompt is None:
            return
        model = self.registryValue("picModel", msg.channel, irc.network)
        timeout = self.registryValue("timeoutSec")
        original = prompt
        prompt = self._expand_prompt(prompt)
        if prompt != original:
            irc.reply("prompt: " + prompt, prefixNick=False)
        try:
            url = self._runware_image(prompt, model, timeout)
        except CreateError as e:
            self.log.warning("Create.pic: %s", e)
            irc.error(str(e))
            return
        irc.reply(self._shorten(url), prefixNick=False)

    pic = wrap(pic, ["public", "text"])

    # -------------------------------------------------------------- !picnsfw

    def picnsfw(self, irc, msg, args, prompt):
        """<prompt> — generate an image via Runware.ai (unrestricted)."""
        if not self._check_cap(irc, msg, "generative"):
            return
        prompt = self._parse_prompt(irc, prompt, "picnsfw")
        if prompt is None:
            return
        model = self.registryValue("model", msg.channel, irc.network)
        timeout = self.registryValue("timeoutSec")
        original = prompt
        prompt = self._expand_prompt(prompt)
        if prompt != original:
            irc.reply("prompt: " + prompt, prefixNick=False)
        try:
            url = self._runware_image(prompt, model, timeout)
        except CreateError as e:
            self.log.warning("Create.picnsfw: %s", e)
            irc.error(str(e))
            return
        irc.reply(NSFW_PREFIX + " " + self._shorten(url), prefixNick=False)

    picnsfw = wrap(picnsfw, ["public", "text"])

    # ------------------------------------------------------------------ !video

    def video(self, irc, msg, args, prompt):
        """<prompt> — Runware (SFW) image → Atlas Wan 2.2 Turbo I2V."""
        if not self._check_cap(irc, msg, "generative"):
            return
        prompt = self._parse_prompt(irc, prompt, "video")
        if prompt is None:
            return
        image_model = self.registryValue("picModel", msg.channel, irc.network)
        image_timeout = self.registryValue("timeoutSec")
        video_timeout = self.registryValue("videoTimeoutSec")
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
        """<prompt> — Runware NSFW image → Atlas Cloud Wan 2.2 Turbo Spicy I2V."""
        if not self._check_cap(irc, msg, "generative"):
            return
        prompt = self._parse_prompt(irc, prompt, "videonsfw")
        if prompt is None:
            return
        image_model = self.registryValue("model", msg.channel, irc.network)
        image_timeout = self.registryValue("timeoutSec")
        video_timeout = self.registryValue("videoTimeoutSec")
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

    # ------------------------------------------------------------------ !cap / !uncap

    def _resolve_user(self, irc, nick):
        try:
            hostmask = irc.state.nickToHostmask(nick)
        except KeyError:
            return None, "nick not in channel"
        try:
            return ircdb.users.getUser(hostmask), None
        except KeyError:
            return None, "user not registered with the bot"

    def cap(self, irc, msg, args, nick, capname):
        """<nick> [capname] — grant capability (default: generative) in this channel."""
        if not msg.channel:
            irc.error("channel-only")
            return
        user, err = self._resolve_user(irc, nick)
        if err:
            irc.error(err)
            return
        cap = ircdb.makeChannelCapability(msg.channel, capname or "generative")
        user.addCapability(cap)
        ircdb.users.setUser(user)
        irc.reply("granted %s to %s" % (cap, nick), prefixNick=False)

    cap = wrap(cap, [("checkCapability", "admin"), "public", "somethingWithoutSpaces", optional("somethingWithoutSpaces")])

    def uncap(self, irc, msg, args, nick, capname):
        """<nick> [capname] — revoke capability (default: generative) in this channel."""
        if not msg.channel:
            irc.error("channel-only")
            return
        user, err = self._resolve_user(irc, nick)
        if err:
            irc.error(err)
            return
        cap = ircdb.makeChannelCapability(msg.channel, capname or "generative")
        user.removeCapability(cap)
        ircdb.users.setUser(user)
        irc.reply("revoked %s from %s" % (cap, nick), prefixNick=False)

    uncap = wrap(uncap, [("checkCapability", "admin"), "public", "somethingWithoutSpaces", optional("somethingWithoutSpaces")])


Class = Create
