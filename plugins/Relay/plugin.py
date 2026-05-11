import supybot.callbacks as callbacks
import supybot.ircmsgs as ircmsgs

class Relay(callbacks.Plugin):
    """Relays public chat from #oldnews to #fatkids."""

    def doPrivmsg(self, irc, msg):
        channel = msg.args[0].lower()
        if channel != '#oldnews':
            return
        if ircmsgs.isCtcp(msg):
            return
        nick = msg.nick
        text = msg.args[1]
        relay_msg = '<%s:#oldnews> %s' % (nick, text)
        irc.queueMsg(ircmsgs.privmsg('#fatkids', relay_msg))

Class = Relay
