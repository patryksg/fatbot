"""
Wikibear: !wikibear — Wiki Bear, the talking teddy toy from the Conan
O'Brien bit, recites a real Wikipedia fact and pivots from harmless trivia
into something weird / creepy / gruesome / nihilistic, deadpan and chipper.
With a question argument, Wiki Bear answers the question with a Wikipedia
source, then segues "Speaking of X..." into a tangential horror fact with
its own source.

Uses the claude CLI with WebSearch (Pro OAuth). Long URLs are shortened via
is.gd through the WARP SOCKS5 proxy on 127.0.0.1:40000.
"""

import os
import re
import subprocess

import supybot.ircdb as ircdb
import supybot.callbacks as callbacks
from supybot.commands import wrap, optional

CLAUDE_BIN = "/home/botuser/.local/bin/claude"
CLAUDE_CONFIG_DIR = "/home/botuser/runbot/.claude"
CLAUDE_MODEL = "claude-opus-4-7"
SIGNOFF = "I'm wikibeaaaaaaaaaaar!!"

WARP_SOCKS = "127.0.0.1:40000"

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
    "Hard rules: 2-4 IRC messages total — aim for the fuller treatment, "
    "not the one-liner. Prefer 3 messages when there's enough material. "
    f"The closing line '{SIGNOFF}' is MANDATORY and must always appear "
    "as the final line on its own; if you run out of room, use a 4th "
    "message just for the signoff. Each message under 380 chars. "
    "Separate IRC messages with a single blank line. Never include URLs "
    "or links of any kind. No preamble, no quotes, no meta, no "
    "warnings, no markdown."
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
    "Hard rules: 2-4 IRC messages total — aim for the fuller treatment, "
    "not the one-liner. Prefer 3 messages when there's enough material. "
    f"The closing line '{SIGNOFF}' is MANDATORY and must always appear "
    "as the final line on its own; if you run out of room, use a 4th "
    "message just for the signoff. Each message under 380 chars. "
    "Separate IRC messages with a single blank line. Never include URLs "
    "or links of any kind. No preamble, no quotes, no meta, no "
    "warnings, no markdown."
)


def _shorten_url(url: str, timeout: int = 6) -> str:
    """Shorten a single URL via is.gd through WARP SOCKS5. Falls back to original."""
    if not url or len(url) < 30:
        return url
    try:
        # is.gd simple API; route through WARP socks proxy.
        r = subprocess.run(
            [
                "curl", "-fsS",
                "--max-time", str(timeout),
                "--socks5-hostname", WARP_SOCKS,
                "--get",
                "--data-urlencode", f"url={url}",
                "--data-urlencode", "format=simple",
                "https://is.gd/create.php",
            ],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        if r.returncode == 0:
            short = r.stdout.strip()
            if short.startswith("http"):
                return short
    except Exception:
        pass
    return url


_URL_RE = re.compile(r"https?://[^\s)>\]]+")


def _shorten_inline(line: str) -> str:
    """Replace every URL in a line with its is.gd short form."""
    def sub(m):
        return _shorten_url(m.group(0))
    return _URL_RE.sub(sub, line)


class Wikibear(callbacks.Plugin):
    """!wikibear — wiki bear shares creepy/absurd Wikipedia factoids."""

    threaded = True

    def wikibear(self, irc, msg, args, question):
        """[<question>] — wiki bear shares an absurd Wikipedia factoid, or
        answers a question with a creepy tangentially-related extra."""
        if not msg.channel:
            irc.error("channel-only")
            return
        chan_cap = ircdb.makeChannelCapability(msg.channel, "wikibear")
        if not ircdb.checkCapability(msg.prefix, chan_cap):
            irc.errorNoCapability(chan_cap)
            return
        timeout = self.registryValue("timeoutSec")

        if question:
            system_prompt = SYSTEM_PROMPT_QUESTION
            stdin_payload = question.strip()
        else:
            system_prompt = SYSTEM_PROMPT_NOARG
            stdin_payload = "go"

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

        # Split into IRC messages on blank lines. Cap at 4 (signoff may
        # need its own message). Within each message keep newlines so
        # multi-line content stays together.
        chunks = [c.strip() for c in re.split(r"\n\s*\n", out) if c.strip()]
        chunks = chunks[:4]
        emitted_lines = []
        for chunk in chunks:
            for line in chunk.split("\n"):
                line = line.strip()
                if not line:
                    continue
                line = _URL_RE.sub("", line).strip()
                if not line:
                    continue
                irc.reply(line, prefixNick=False)
                emitted_lines.append(line)
        # Guarantee the signoff is present, even as a 4th/standalone msg.
        if not any(SIGNOFF in l for l in emitted_lines):
            irc.reply(SIGNOFF, prefixNick=False)

    wikibear = wrap(wikibear, ["public", optional("text")])


Class = Wikibear
