import supybot.callbacks as callbacks
import supybot.ircmsgs as ircmsgs


def _parse_modes(s):
    out = []
    sign = '+'
    for ch in s:
        if ch in '+-':
            sign = ch
        elif ch.isalpha():
            out.append((sign, ch))
    return out


class ChanModes(callbacks.Plugin):
    """Auto-enforces configured channel modes when the bot has ops.

    Set per channel:  !config channel #foo plugins.ChanModes.modes +pnst
    Disable:          !config channel #foo plugins.ChanModes.modes ""
    """

    def _enforce(self, irc, channel):
        if not channel or not channel.startswith(('#', '&', '+', '!')):
            return
        desired = self.registryValue('modes', channel)
        if not desired:
            return
        state = irc.state.channels.get(channel)
        if state is None:
            return
        if irc.nick not in state.ops:
            return
        current = state.modes
        plus, minus = [], []
        for sign, letter in _parse_modes(desired):
            has = letter in current
            if sign == '+' and not has:
                plus.append(letter)
            elif sign == '-' and has:
                minus.append(letter)
        if not plus and not minus:
            return
        modestr = ''
        if plus:
            modestr += '+' + ''.join(plus)
        if minus:
            modestr += '-' + ''.join(minus)
        irc.queueMsg(ircmsgs.IrcMsg(command='MODE', args=(channel, modestr)))

    def do366(self, irc, msg):
        # End of /NAMES — bot just finished joining
        channel = msg.args[1]
        self._enforce(irc, channel)

    def doMode(self, irc, msg):
        # Re-assert when a mode change happens (covers regaining ops)
        if not msg.args:
            return
        channel = msg.args[0]
        self._enforce(irc, channel)


Class = ChanModes
