import supybot.callbacks as callbacks
from supybot.commands import wrap

REPO_URL = "https://github.com/patryksg/fatbot/tree/master"
INSTALL_URL = "https://gist.github.com/patryksg/3584bc2ce12d451e219d87c3d8b85a5a"


class Repo(callbacks.Plugin):
    """Replies with the GitHub repo URL or install guide for this bot."""

    threaded = False

    def repo(self, irc, msg, args):
        """takes no arguments

        Reply with the GitHub repo URL for fatbot.
        """
        irc.reply(REPO_URL)
    repo = wrap(repo)

    def howto(self, irc, msg, args):
        """takes no arguments

        Reply with the link to the setup guide (Debian Trixie from scratch).
        """
        irc.reply("Setup guide (Debian Trixie from scratch): " + INSTALL_URL)
    howto = wrap(howto)


Class = Repo
