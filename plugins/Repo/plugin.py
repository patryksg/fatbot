import supybot.callbacks as callbacks
from supybot.commands import wrap

REPO_URL = "https://github.com/patryksg/fatbot/tree/master"


class Repo(callbacks.Plugin):
    """Replies with the GitHub repo URL for this bot."""

    threaded = False

    def repo(self, irc, msg, args):
        """takes no arguments

        Reply with the GitHub repo URL for fatbot.
        """
        irc.reply(REPO_URL)
    repo = wrap(repo)


Class = Repo
