import json
import os
import tempfile
import threading
import supybot.callbacks as callbacks
import supybot.ircmsgs as ircmsgs
import supybot.conf as conf
from supybot.commands import wrap

class Greeter(callbacks.Plugin):
    """Greets registered users when they join #yourchannel."""

    def __init__(self, irc):
        super().__init__(irc)
        self.datafile = conf.supybot.directories.data.dirize("Greeter.json")
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if os.path.exists(self.datafile):
            with open(self.datafile) as f:
                self.greetings = json.load(f)
        else:
            self.greetings = {}

    def _save(self):
        d = os.path.dirname(self.datafile) or "."
        fd, tmp = tempfile.mkstemp(prefix=".Greeter.", suffix=".json", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.greetings, f, indent=2)
            os.replace(tmp, self.datafile)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def addgreet(self, irc, msg, args, nick, greeting):
        """<nick> <greeting> -- Adds a greeting for <nick> when they join #yourchannel."""
        with self._lock:
            self.greetings[nick.lower()] = greeting
            self._save()
        irc.replySuccess()
    addgreet = wrap(addgreet, [("checkCapability", "admin"), "something", "text"])

    def delgreet(self, irc, msg, args, nick):
        """<nick> -- Removes the greeting for <nick>."""
        with self._lock:
            if nick.lower() not in self.greetings:
                irc.reply("No greeting found for %s." % nick)
                return
            del self.greetings[nick.lower()]
            self._save()
        irc.replySuccess()
    delgreet = wrap(delgreet, [("checkCapability", "admin"), "something"])

    def listgreets(self, irc, msg, args):
        """Lists all stored greetings."""
        if not self.greetings:
            irc.reply("No greetings stored.")
            return
        entries = ["[%s] %s" % (n, g) for n, g in sorted(self.greetings.items())]
        irc.reply(", ".join(entries))
    listgreets = wrap(listgreets, [("checkCapability", "admin")])

    def doJoin(self, irc, msg):
        channel = msg.args[0]
        if channel.lower() != "#yourchannel":
            return
        if msg.nick == irc.nick:
            return
        greeting = self.greetings.get(msg.nick.lower())
        if greeting:
            irc.queueMsg(ircmsgs.privmsg(channel, greeting))

Class = Greeter
