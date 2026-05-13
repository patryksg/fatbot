"""
Create: image generation via Runware.ai.

Usage in a channel where the caller holds the #chan,create capability:
    !create <prompt>

Returns a Runware-hosted URL for the generated image.
"""

import os
import uuid
import json
import urllib.request
import urllib.error

import supybot.conf as conf
import supybot.ircdb as ircdb
import supybot.callbacks as callbacks
from supybot.commands import wrap

CAPABILITY = 'create'
RUNWARE_ENDPOINT = "https://api.runware.ai/v1"


class CreateError(Exception):
    pass


class Create(callbacks.Plugin):
    """!create <prompt> — generate an image with Runware.ai and post the URL."""

    threaded = True

    def _runware_generate(self, prompt, model, timeout):
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
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise CreateError("runware http %d: %s" % (e.code, detail))
        except Exception as e:
            raise CreateError("runware request failed: %s" % e)
        try:
            j = json.loads(data.decode("utf-8"))
        except Exception:
            raise CreateError("runware returned non-JSON: %s" % data[:200])
        errs = j.get("errors") or []
        if errs:
            msg = errs[0].get("code") or errs[0].get("message") or str(errs[0])
            raise CreateError("runware: " + msg[:200])
        results = j.get("data") or []
        if not results or "imageURL" not in results[0]:
            raise CreateError("runware response had no imageURL: %s"
                              % json.dumps(j)[:200])
        return results[0]["imageURL"]

    def create(self, irc, msg, args, prompt):
        """<prompt>

        Generate an image via Runware.ai and post the URL.
        """
        if not msg.channel:
            irc.error("channel-only")
            return
        chan_cap = ircdb.makeChannelCapability(msg.channel, CAPABILITY)
        if not ircdb.checkCapability(msg.prefix, chan_cap):
            irc.errorNoCapability(chan_cap)
            return
        prompt = prompt.strip()
        if not prompt:
            irc.error("usage: !create <prompt>")
            return
        if len(prompt) > 4000:
            irc.error("prompt too long")
            return
        model = self.registryValue('model', msg.channel, irc.network)
        timeout = self.registryValue('timeoutSec')
        try:
            url = self._runware_generate(prompt, model, timeout)
        except CreateError as e:
            self.log.warning("Create: runware fail: %s", e)
            irc.error(str(e))
            return
        irc.reply(url, prefixNick=False)

    create = wrap(create, ['public', 'text'])


Class = Create
