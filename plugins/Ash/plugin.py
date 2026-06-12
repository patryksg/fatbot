"""
Ash: !ashnormal / !ashsmart — Bruce Campbell's Ash Williams
(Evil Dead / Army of Darkness / Ash vs Evil Dead) drops in-character lines.
Two tiers gated on separate per-user channel-caps.

  ashnormal  -> Claude Haiku (cheap, fast)
  ashsmart   -> Claude Haiku (same for now; bump MODEL_SMART to re-split tiers)
"""

import os
import re
import subprocess

import supybot.ircdb as ircdb
import supybot.callbacks as callbacks
from supybot.commands import wrap, optional

CLAUDE_BIN = "/home/botuser/.local/bin/claude"
CLAUDE_CONFIG_DIR = "/home/botuser/runbot/.claude"
MODEL_NORMAL = "claude-haiku-4-5-20251001"
MODEL_SMART = "claude-haiku-4-5-20251001"
TIMEOUT_SEC = 120
MAX_LINES = 8
MAX_CHARS = 420

CAPS = {"normal": "ashnormal", "smart": "ashsmart"}

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
    "Punchy, like a Bruce Campbell line read. 1-8 IRC lines, each under "
    "380 chars. No preamble, no 'Ash:', no quote marks around your line, "
    "no narration."
)

SYSTEM_PROMPT_QUESTION = (
    PERSONALITY +
    "\n\nTask: the user asked Ash something. Answer fully in character. "
    "Stay in the bit even for serious factual questions — Ash bullshits "
    "through anything with swagger and gets there mostly by accident. "
    "If it's banter or a what-if scenario, banter harder. 1-8 IRC lines, "
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


def _pack_balanced(text, hard_cap):
    """Pack text into the FEWEST messages whose lengths come out as even as
    possible, each at most hard_cap chars. Collapses the model's own line
    breaks; hard-splits a token longer than hard_cap. Picking the minimum
    message count and then splitting evenly avoids the greedy 'first message
    crammed full, rest half-empty' look (825 chars -> [413, 412], not
    [380, 221, 224])."""
    words = []
    for w in text.split():
        while len(w) > hard_cap:
            words.append(w[:hard_cap])
            w = w[hard_cap:]
        if w:
            words.append(w)
    if not words:
        return []
    total = sum(len(w) for w in words) + len(words) - 1
    n = max(1, -(-total // hard_cap))
    while True:
        target = total / n
        chunks, cur = [], ""
        for w in words:
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= hard_cap and (
                    len(chunks) == n - 1
                    or len(cur) + 1 + len(w) <= target):
                cur += " " + w
            else:
                chunks.append(cur)
                cur = w
        if cur:
            chunks.append(cur)
        if len(chunks) <= n:
            return chunks
        n = len(chunks)


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
    # Re-flow Ash's prose: collapse his own line breaks and repack into the
    # FEWEST, evenly-sized messages (see _pack_balanced) so we never get one
    # full message trailed by half-empty ones. Only the last line is
    # ellipsized, and only on real overflow past MAX_LINES.
    pieces = _pack_balanced(" ".join(cleaned), MAX_CHARS)
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


class Ash(callbacks.Plugin):
    """!ashnormal/!ashsmart — Ash Williams from Evil Dead, in character."""

    threaded = True

    def _run(self, irc, msg, question, mode):
        cap_name = CAPS[mode]
        if not _check_cap(irc, msg, cap_name):
            return
        system_prompt = SYSTEM_PROMPT_QUESTION if question else SYSTEM_PROMPT_NOARG
        if mode == "smart":
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
        """[<question>] — Ash answers via Claude (smart tier)."""
        self._run(irc, msg, question, "smart")
    ashsmart = wrap(ashsmart, ["public", optional("text")])


Class = Ash
