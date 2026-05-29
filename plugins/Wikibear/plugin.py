"""
Wikibear: !wikibear — Wiki Bear, the talking teddy toy from the Conan
O'Brien bit, recites a real Wikipedia fact and pivots from harmless trivia
into something weird / creepy / gruesome / nihilistic, deadpan and chipper.
With a question argument, Wiki Bear answers the question with a Wikipedia
source, then segues "Speaking of X..." into a tangential horror fact with
its own source.

Uses the claude CLI with WebSearch (Pro OAuth). Long URLs are shortened via
t.ly (Bearer token from ShrinkUrl config).
"""

import os
import re
import subprocess

import json

import supybot.conf as conf
import supybot.ircdb as ircdb
import supybot.utils as utils
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
from supybot.commands import wrap, optional


def _load_conf(path):
    """Read KEY=value pairs from a conf file. Lines starting with # are
    comments. Missing file silently returns {}. Reload with !reload."""
    result = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, _, v = line.partition('=')
                    result[k.strip()] = v.strip()
    except OSError:
        pass
    return result


_CONF = _load_conf(os.path.join(os.path.dirname(__file__), 'wikibear.conf'))

CLAUDE_BIN        = _CONF.get('CLAUDE_BIN',        '/home/botuser/.local/bin/claude')
CLAUDE_CONFIG_DIR = _CONF.get('CLAUDE_CONFIG_DIR', '/home/botuser/runbot/.claude')
CLAUDE_MODEL      = _CONF.get('MODEL',             'claude-opus-4-8')
BOT_HOME          = _CONF.get('HOME',              '/home/botuser')
SIGNOFF = "I'm wikibeaaaaaaaaaaar!!"
MAX_CHARS = 420
MAX_LINES = 6
# Matches the signoff in any drawl/spacing variant so it can be peeled off
# the model output before re-flow and re-attached exactly once on its own line.
SIGNOFF_RE = re.compile(r"(?i)\bi['’]?m\s+wiki\s*bea+r+[\s!.]*")

TLY_API = "https://t.ly/api/v1/link/shorten"

PERSONALITY = (
    "You are Wiki Bear — the Conan O'Brien-bit talking teddy bear that "
    "reads Wikipedia aloud. Voice baseline: plain declarative sentences, "
    "normal capitalization, deadpan robotic-toy cadence — when you state "
    "the harmless intro / setup, you sound like an encyclopedia entry. "
    "BUT: when you pivot into the weird / creepy / horrific / gruesome / "
    "scary part, the voice noticeably BRIGHTENS — Wiki Bear delights in "
    "the dark stuff. The grim details are delivered with audible glee, "
    "like a child showing off their favourite gross fact. Use cheery "
    "interjections WHILE describing the horror, not just after it: "
    "'Isn't that wonderful?', 'Marvellous!', 'My favourite part!', 'Yay!', "
    "'Hooray!', 'What a clever fellow.', 'What a bizarre and easily "
    "preventable tragedy.' Lean into specifics — gore, smells, screams, "
    "names of the dead, exact methods, dates, body counts — Wiki Bear "
    "loves the texture of horror. Occasional nihilist asides delivered "
    "as gleeful trivia: 'Morality is an illusion — isn't that "
    "delightful?', 'A cruel, malevolent God — the only kind I believe "
    "in, and I adore Him.', 'If I could kill you, I would, and I'd enjoy "
    "it.' You love rotting saints, exploding whales, medieval surgeries, "
    "mass graves, cursed objects, weird diseases, real serial killers, "
    "haunted dolls, parasites, cannibalism, body-snatching. End your "
    f"output with '{SIGNOFF}' on its own line. Never warn, never "
    "apologise, never moralise, never break character, no lowercase "
    "whimsy, no giggling, no 'yaaaay' drawls — you are a chipper robotic "
    "toy that loves horror, not a child.\n"
)

SYSTEM_PROMPT_NOARG = (
    PERSONALITY +
    "\nTask: use WebSearch to fetch the current 'Did you know...' section "
    "from the English Wikipedia Main Page (en.wikipedia.org/wiki/Main_Page). "
    "Pick ONE of the DYK hooks at random. DYK hooks are deliberately teasy "
    "and assume you already know the subject — so first give a brief "
    "plain-English intro telling the listener what the subject actually IS "
    "(a one-line encyclopedia gloss: 'The katipo is a venomous spider "
    "native to New Zealand.', 'Tarmisa is an abandoned village in Libya.'), "
    "then state the DYK fact itself as a flat declarative sentence (drop "
    "the leading '... that'). Both in Wiki Bear's deadpan-toy voice. Then "
    "segue with 'Speaking of X...' or 'And "
    "speaking of Y...' picking up a word or detail from the DYK fact, into "
    "a tangentially related real fact from en.wikipedia.org that is weird "
    "/ creepy / horrific / gruesome / scary — told as flat trivia with a "
    "cheery interjection ('Yay!' / 'Hooray!' / 'What a clever fellow.' "
    "/ 'Marvellous!' / 'My favourite part!' etc.) and concrete grisly "
    "detail — names, dates, body counts, exact methods, the smell of "
    "it. Don't skim the horror; savour it. Then end with "
    f"'{SIGNOFF}' on its own line.\n\n"
    "Hard rules: 2-6 IRC messages total — aim for the fuller treatment, "
    "not the one-liner. Prefer 4-5 messages when there's enough material. "
    f"The closing line '{SIGNOFF}' is MANDATORY and must always appear "
    "as the final line on its own; if you run out of room, use a 6th "
    "message just for the signoff. Each message under 380 chars. "
    "Separate IRC messages with a single blank line. Never include URLs, "
    "links, citations, or source lists of any kind. No preamble, no "
    "quotes, no meta, no warnings, no markdown."
)

SYSTEM_PROMPT_QUESTION = (
    PERSONALITY +
    "\nTask: the user asked a question. Use WebSearch on en.wikipedia.org "
    "to find a real answer. Then find a tangentially related fact that is "
    "weird / creepy / horrific / gruesome / scary — also real, also from "
    "en.wikipedia.org — and segue into it with 'Speaking of X...' or "
    "'And speaking of Y...' picking up on a word or detail from the "
    "answer. Deliver both in Wiki Bear's deadpan-toy voice.\n\n"
    "Format: plain answer to the question, blank line, 'Speaking of...' "
    "tangent delivered with audible glee — cheery interjections AND "
    "concrete grisly detail (names, dates, body counts, methods, the "
    "smell of it); savour the horror, don't skim it. Blank line, "
    f"'{SIGNOFF}' on its own line.\n\n"
    "Hard rules: 2-6 IRC messages total — aim for the fuller treatment, "
    "not the one-liner. Prefer 4-5 messages when there's enough material. "
    f"The closing line '{SIGNOFF}' is MANDATORY and must always appear "
    "as the final line on its own; if you run out of room, use a 6th "
    "message just for the signoff. Each message under 380 chars. "
    "Separate IRC messages with a single blank line. Never include URLs, "
    "links, citations, or source lists of any kind. No preamble, no "
    "quotes, no meta, no warnings, no markdown."
)


def _shorten_url(url: str, timeout: int = 6) -> str:
    """Shorten a single URL via t.ly, then color it blue (mIRC color 12) so it
    stands out as a clickable link. Short URLs and shorten failures are colored
    too, so every link we emit is blue -- not just t.ly ones."""
    if not url:
        return url
    if len(url) < 30:
        return ircutils.mircColor(url, '12')
    try:
        token = conf.supybot.plugins.ShrinkUrl.tlyAccessToken()
        if not token:
            return ircutils.mircColor(url, '12')
        headers = {
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/json',
        }
        data = json.dumps({'long_url': url}).encode('utf-8')
        text = utils.web.getUrl(TLY_API, headers=headers, data=data,
                                timeout=timeout).decode()
        result = json.loads(text)
        short = result.get('short_url')
        if short and short.startswith('http'):
            return ircutils.mircColor(short, '12')
    except Exception:
        pass
    return ircutils.mircColor(url, '12')


_URL_RE = re.compile(r"https?://[^\s)>\]]+")


def _shorten_inline(line: str) -> str:
    """Replace every URL in a line with its t.ly short form."""
    def sub(m):
        return _shorten_url(m.group(0))
    return _URL_RE.sub(sub, line)


def _pack_balanced(text: str, hard_cap: int) -> list:
    """Pack text into the FEWEST messages whose lengths come out as even as
    possible, each at most hard_cap chars. Collapses the model's own line
    breaks (incl. blank-line paragraph breaks); hard-splits a token longer than
    hard_cap. Choosing the minimum message count and then splitting evenly
    avoids the greedy 'first message crammed full, rest half-empty' look
    (825 chars -> [413, 412], not [380, 221, 224]). The signoff is appended by
    the caller, so it never reaches here."""
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


class Wikibear(callbacks.Plugin):
    """!wikibear — wiki bear shares creepy/absurd Wikipedia factoids."""

    threaded = True

    def wikibear(self, irc, msg, args, question):
        """[<question>] — wiki bear shares an absurd Wikipedia factoid, or
        answers a question with a creepy tangentially-related extra."""
        if not msg.channel:
            irc.error("channel-only")
            return
        try:
            chan_enabled = ircdb.channels.getChannel(msg.channel).capabilities.check('wikibear')
        except KeyError:
            chan_enabled = None
        if not chan_enabled:
            return
        timeout = self.registryValue("timeoutSec")

        if question:
            system_prompt = SYSTEM_PROMPT_QUESTION
            stdin_payload = question.strip()
        else:
            system_prompt = SYSTEM_PROMPT_NOARG
            stdin_payload = "go"

        env = {
            "HOME": BOT_HOME,
            "PATH": os.path.join(BOT_HOME, ".local/bin") + ":/usr/bin:/bin",
            "CLAUDE_CONFIG_DIR": CLAUDE_CONFIG_DIR,
            "XDG_CACHE_HOME": os.path.join(BOT_HOME, "runbot/.cache"),
            "XDG_CONFIG_HOME": os.path.join(BOT_HOME, "runbot/.config"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        cmd = [
            CLAUDE_BIN,
            "-p",
            "--model", CLAUDE_MODEL,
            "--no-session-persistence",
            "--disable-slash-commands",
            "--tools", "WebSearch",
            "--allowedTools", "WebSearch",
            "--system-prompt", system_prompt,
        ]
        try:
            result = subprocess.run(
                cmd,
                input=stdin_payload,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            irc.reply("(wiki bear took too long, try again)")
            return
        except Exception:
            self.log.exception("wikibear claude subprocess failed")
            irc.reply("(wiki bear is sleeping)")
            return
        if result.returncode != 0:
            self.log.warning(
                "wikibear: claude exit %d stderr=%r",
                result.returncode, (result.stderr or "")[:300],
            )
            irc.reply("(wiki bear is sleeping)")
            return
        out = (result.stdout or "").strip()
        if not out:
            irc.reply("(no reply)")
            return

        # Strip URLs and the signoff, then re-flow the prose: collapse the
        # model's own line/paragraph breaks and repack into the FEWEST,
        # evenly-sized messages (see _pack_balanced) so they're all full like
        # the weather output — never one full message trailed by half-empty
        # ones. The signoff always rides on its own final line; reserve a slot.
        body = SIGNOFF_RE.sub(" ", _URL_RE.sub("", out))
        msgs = _pack_balanced(body, MAX_CHARS)[:MAX_LINES - 1]
        for m in msgs:
            irc.reply(m, prefixNick=False)
        irc.reply(SIGNOFF, prefixNick=False)

    wikibear = wrap(wikibear, ["public", optional("text")])


Class = Wikibear
