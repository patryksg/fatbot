import supybot.callbacks as callbacks
import supybot.ircmsgs as ircmsgs
from supybot.commands import wrap, additional


class TopicLock(callbacks.Plugin):
    """Snapshot a channel's topic and revert any change until unlocked.

    !topic lock [<channel>] [<new topic>]  set <new topic> (if given), then guard it
    !topic unlock [<channel>]              release the guard
    !topic status [<channel>]              show whether a channel is locked
    """

    threaded = False

    def __init__(self, irc):
        super().__init__(irc)
        self._locked = {}

    def doTopic(self, irc, msg):
        if len(msg.args) < 2:
            return
        channel, new_topic = msg.args[0], msg.args[1]
        if msg.nick == irc.nick:
            return
        key = channel.lower()
        if key not in self._locked:
            return
        locked = self._locked[key]
        if new_topic == locked:
            return
        irc.queueMsg(ircmsgs.topic(channel, locked))

    def topic(self, irc, msg, args, action, channel, newtopic):
        """<lock|unlock|status> [<channel>] [<new topic>]

        Snapshot <channel>'s current topic and revert any change (lock),
        release the snapshot (unlock), or show status. With lock, an
        optional <new topic> is set first (needs ops) and then locked.
        """
        action = action.lower()
        key = channel.lower()
        if action == 'lock':
            state = irc.state.channels.get(channel)
            if state is None:
                irc.error("I'm not in %s." % channel, Raise=True)
            if newtopic:
                irc.queueMsg(ircmsgs.topic(channel, newtopic))
                current = newtopic
            else:
                current = state.topic or ''
            self._locked[key] = current
            irc.reply("Topic locked for %s: %r" % (channel, current))
        elif action == 'unlock':
            if self._locked.pop(key, None) is None:
                irc.reply("%s wasn't locked." % channel)
            else:
                irc.reply("Topic unlocked for %s." % channel)
        elif action == 'status':
            locked = self._locked.get(key)
            if locked is None:
                irc.reply("%s is not locked." % channel)
            else:
                irc.reply("%s is locked to: %r" % (channel, locked))
        else:
            irc.error("unknown action %r; use lock|unlock|status" % action)
    topic = wrap(topic, ['something', 'channel', additional('text')])


Class = TopicLock
