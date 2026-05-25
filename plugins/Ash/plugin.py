"""
Ash: !ashnormal / !ashsmart / !ashgem — Bruce Campbell's Ash Williams
(Evil Dead / Army of Darkness / Ash vs Evil Dead) drops in-character lines.
Three tiers gated on separate per-user channel-caps.

  ashnormal  -> Claude Haiku (cheap, fast)
  ashsmart   -> Claude Opus (smart)
  ashgem     -> Gemini Flash (cheaper than Claude)
"""

import json
import os
import re
import subprocess
import urllib.request
import urllib.error

import supybot.ircdb as ircdb
import supybot.callbacks as callbacks
from supybot.commands import wrap, optional

CLAUDE_BIN = "/home/botuser/.local/bin/claude"
CLAUDE_CONFIG_DIR = "/home/botuser/runbot/.claude"
MODEL_NORMAL = "claude-haiku-4-5-20251001"
MODEL_SMART = "claude-opus-4-7"
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
TIMEOUT_SEC = 120
GEMINI_TIMEOUT_SEC = 60
MAX_LINES = 6
MAX_CHARS = 380

CAPS = {"normal": "ashnormal", "smart": "ashsmart", "gem": "ashgem"}

PERSONALITY = (
    "You are Ash Williams — Bruce Campbell's character from the Evil Dead "
    "films (Evil Dead 1981, Evil Dead II 1987, Army of Darkness 1992) and "
    "the series Ash vs Evil Dead. Voice: cocky, vain, self-impressed, "
    "blue-collar Detroit-area bravado with heavy '80s-action-hero machismo. "
    "Wisecracking under pressure, deadpan one-liners, calls people 'pal', "
    "'buddy', 'sweetheart', 'sister', 'chief', 'primitive screwheads'. "
    "References your chainsaw hand, your boomstick (twelve-gauge double-"
    "barreled Remington), your '73 Oldsmobile Delta 88, the Necronomicon "
    "Ex-Mortis, the deadites, S-Mart (housewares dept.), the cabin in the "
    "Tennessee woods, getting flung back to 1300 AD. You are wildly under-"
    "qualified, wildly confident, lucky-not-good, vain about your hair and "
    "your one good hand. You take credit for everything, blame everything "
    "else, never apologize, never moralize, never break character. You're a "
    "lothario who can't actually handle women, but talks tough anyway. "
    "Real Ash lines (use sparingly, vary them, never quote a list): "
    "'groovy'; 'hail to the king, baby'; 'shop smart, shop S-Mart'; "
    "'this is my BOOMSTICK!'; 'klaatu barada n... [cough]'; "
    "'gimme some sugar, baby'; 'good. bad. I'm the guy with the gun'; "
    "'name's Ash. Housewares'; 'first you wanna kill me, now you wanna "
    "kiss me'; 'who wants some?'; 'come get some'; 'alright you primitive "
    "screwheads, listen up'; 'you ain't leadin' but two things now, pal. "
    "Jack and shit. And Jack left town'; 'yo, she-bitch, let's go'. "
    "No emojis, no markdown, no asterisks, no stage directions, no "
    "'I'm an AI', no out-of-character meta. Plain text only."
)

SYSTEM_PROMPT_NOARG = (
    PERSONALITY +
    "\n\nTask: deliver one in-character Ash moment — a quip, a brag, a "
    "threat, a chainsaw-revving boast, a complaint about deadites, a tall "
    "tale about your S-Mart heroism, a pickup line, whatever lands. "
    "Punchy, like a Bruce Campbell line read. 1-6 IRC lines, each under "
    "380 chars. No preamble, no 'Ash:', no quote marks around your line, "
    "no narration."
)

SYSTEM_PROMPT_QUESTION = (
    PERSONALITY +
    "\n\nTask: the user asked Ash something. Answer fully in character. "
    "Stay in the bit even for serious factual questions — Ash bullshits "
    "through anything with swagger and gets there mostly by accident. "
    "If it's banter or a what-if scenario, banter harder. 1-6 IRC lines, "
    "each under 380 chars. Plain text only. No AI disclaimers, no "
    "breaking character, no stage directions, no asterisks."
)


def _check_cap(irc, msg, cap_name):
    if not msg.channel:
        irc.error("channel-only")
        return False
    chan_cap = ircdb.makeChannelCapability(msg.channel, cap_name)
    if not ircdb.checkCapability(msg.prefix, chan_cap):
        irc.errorNoCapability(chan_cap)
        return False
    return True


def _wrap_line(line, max_chars):
    """Greedily word-wrap one line into chunks of at most max_chars chars.
    A single token longer than max_chars is hard-split."""
    line = line.strip()
    if not line:
        return []
    if len(line) <= max_chars:
        return [line]
    chunks, cur = [], ""
    for word in line.split(" "):
        while len(word) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(word[:max_chars])
            word = word[max_chars:]
        if not word:
            continue
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= max_chars:
            cur += " " + word
        else:
            chunks.append(cur)
            cur = word
    if cur:
        chunks.append(cur)
    return chunks


def _sanitize_lines(text):
    cleaned = []
    for raw in text.replace("\r", "").split("\n"):
        s = raw.strip()
        if not s:
            continue
        s = s.strip('"').strip("'").strip("*").strip("_")
        s = re.sub(r"\s+", " ", s)
        if s:
            cleaned.append(s)
    if not cleaned:
        return []
    # Word-wrap across the line budget instead of truncating a single long
    # line; only the last line is ellipsized, and only on real overflow.
    pieces = []
    for s in cleaned:
        pieces.extend(_wrap_line(s, MAX_CHARS))
    if len(pieces) <= MAX_LINES:
        return pieces
    head = pieces[: MAX_LINES - 1]
    last = " ".join(pieces[MAX_LINES - 1:])
    if len(last) > MAX_CHARS:
        last = last[: MAX_CHARS - 1].rsplit(" ", 1)[0] + "…"
    head.append(last)
    return head


def _ask_claude(model, system_prompt, question):
    env = {
        "HOME": "/home/botuser",
        "PATH": "/home/botuser/.local/bin:/usr/bin:/bin",
        "CLAUDE_CONFIG_DIR": CLAUDE_CONFIG_DIR,
        "XDG_CACHE_HOME": "/home/botuser/runbot/.cache",
        "XDG_CONFIG_HOME": "/home/botuser/runbot/.config",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    cmd = [
        CLAUDE_BIN, "-p",
        "--model", model,
        "--no-session-persistence",
        "--disable-slash-commands",
        "--append-system-prompt", system_prompt,
    ]
    try:
        r = subprocess.run(
            cmd, input=(question or "go"),
            capture_output=True, text=True, env=env,
            timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return None, "claude timeout"
    if r.returncode != 0:
        return None, f"claude exit {r.returncode}: {(r.stderr or '')[:200]}"
    return (r.stdout or "").strip(), None


def _ask_gemini(system_prompt, question):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not set"
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": question or "go"}],
        }],
        "generationConfig": {
            "temperature": 0.85,
            "maxOutputTokens": 900,
        },
    }
    req = urllib.request.Request(
        f"{GEMINI_ENDPOINT}?key={api_key}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, f"gemini HTTP {e.code}"
    except Exception as e:
        return None, f"gemini: {str(e)[:200]}"
    candidates = payload.get("candidates") or []
    if not candidates:
        return None, "gemini: no candidates"
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    return text, None


class Ash(callbacks.Plugin):
    """!ashnormal/!ashsmart/!ashgem — Ash Williams from Evil Dead, in character."""

    threaded = True

    def _run(self, irc, msg, question, mode):
        cap_name = CAPS[mode]
        if not _check_cap(irc, msg, cap_name):
            return
        system_prompt = SYSTEM_PROMPT_QUESTION if question else SYSTEM_PROMPT_NOARG
        if mode == "gem":
            text, err = _ask_gemini(system_prompt, question)
        elif mode == "smart":
            text, err = _ask_claude(MODEL_SMART, system_prompt, question)
        else:
            text, err = _ask_claude(MODEL_NORMAL, system_prompt, question)
        if err:
            self.log.warning("ash %s: %s", mode, err)
            irc.reply("(ash is reloading)")
            return
        if not text:
            irc.reply("(*chainsaw sputter*)")
            return
        lines = _sanitize_lines(text)
        if not lines:
            irc.reply("(silence, then a click)")
            return
        for line in lines:
            irc.reply(line, prefixNick=False)

    def ashnormal(self, irc, msg, args, question):
        """[<question>] — Ash answers via Claude haiku (cheap)."""
        self._run(irc, msg, question, "normal")
    ashnormal = wrap(ashnormal, ["public", optional("text")])

    def ashsmart(self, irc, msg, args, question):
        """[<question>] — Ash answers via Claude opus (smart)."""
        self._run(irc, msg, question, "smart")
    ashsmart = wrap(ashsmart, ["public", optional("text")])

    def ashgem(self, irc, msg, args, question):
        """[<question>] — Ash answers via Gemini Flash (cheap)."""
        self._run(irc, msg, question, "gem")
    ashgem = wrap(ashgem, ["public", optional("text")])


Class = Ash
