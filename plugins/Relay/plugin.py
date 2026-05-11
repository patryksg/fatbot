import supybot.callbacks as callbacks
import supybot.ircmsgs as ircmsgs

class Relay(callbacks.Plugin):
    """Relays public chat from #yourchannel2 to #yourchannel."""

    def doPrivmsg(self, irc, msg):
        channel = msg.args[0].lower()
        if channel != '#yourchannel2':
            return
        if ircmsgs.isCtcp(msg):
            return
        nick = msg.nick
        text = msg.args[1]
        relay_msg = '<%s:#yourchannel2> %s' % (nick, text)
        irc.queueMsg(ircmsgs.privmsg('#yourchannel', relay_msg))

Class = Relay
