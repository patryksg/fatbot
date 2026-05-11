import json
import os
import supybot.callbacks as callbacks
import supybot.ircmsgs as ircmsgs
import supybot.conf as conf
from supybot.commands import wrap

class Greeter(callbacks.Plugin):
    """Greets registered users when they join #yourchannel."""

    def __init__(self, irc):
        super().__init__(irc)
        self.datafile = conf.supybot.directories.data.dirize("Greeter.json")
        self._load()

    def _load(self):
        if os.path.exists(self.datafile):
            with open(self.datafile) as f:
                self.greetings = json.load(f)
        else:
            self.greetings = {}

    def _save(self):
        with open(self.datafile, "w") as f:
            json.dump(self.greetings, f, indent=2)

    def addgreet(self, irc, msg, args, nick, greeting):
        """<nick> <greeting> -- Adds a greeting for <nick> when they join #yourchannel."""
        self.greetings[nick.lower()] = greeting
        self._save()
        irc.replySuccess()
    addgreet = wrap(addgreet, ["something", "text"])

    def delgreet(self, irc, msg, args, nick):
        """<nick> -- Removes the greeting for <nick>."""
        if nick.lower() in self.greetings:
            del self.greetings[nick.lower()]
            self._save()
            irc.replySuccess()
        else:
            irc.reply("No greeting found for %s." % nick)
    delgreet = wrap(delgreet, ["something"])

    def listgreets(self, irc, msg, args):
        """Lists all stored greetings."""
        if not self.greetings:
            irc.reply("No greetings stored.")
            return
        entries = ["[%s] %s" % (n, g) for n, g in sorted(self.greetings.items())]
        irc.reply(", ".join(entries))
    listgreets = wrap(listgreets)

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
