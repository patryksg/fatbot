###
# Seen — per-channel activity log + "!seen <nick>" lookup.
#
# Logging is enabled per channel via the 'seen' channel capability:
#     chancap seen      (or the convenience alias:  seenlog on)
#     unchancap seen     (or:  seenlog off)
# When enabled, every message, action, join, part, quit, nick change, kick,
# mode and topic change is appended to <channel>.seen.log under the bot's log
# directory, in the exact same line format as the stock ChannelLogger plugin.
# "!seen <nick>" reads that file back to find a user's most recent activity.
###

import os
import re
import time
from datetime import datetime

import supybot.conf as conf
import supybot.utils as utils
import supybot.ircdb as ircdb
import supybot.ircmsgs as ircmsgs
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
from supybot.commands import wrap, optional


# Matches a ChannelLogger line:  "2026-05-25T04:11:00  <rest of line>"
# (timestamp is supybot.log.timestampFormat = %Y-%m-%dT%H:%M:%S, then 2 spaces)
_LINE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+(.*)$')
_NOTICE_RE = re.compile(r'^-(\S+)- (.*)$')


def _parse_line(line):
    """('2026-05-25T04:11:00  <psg> hi') -> (datetime, '<psg> hi') or None."""
    m = _LINE_RE.match(line)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None
    return (dt, m.group(2).rstrip('\r\n'))


def _format_ago(delta_seconds):
    """Compact relative duration: '45s', '5m', '2h13m', '5d10h35m'.
    Minute resolution above a minute; seconds only under a minute."""
    s = int(delta_seconds)
    if s < 0:
        s = 0
    if s < 60:
        return '%ds' % s
    mins = s // 60
    days, rem = divmod(mins, 1440)
    hours, m = divmod(rem, 60)
    out = ''
    if days:
        out += '%dd' % days
    if hours:
        out += '%dh' % hours
    if m or not out:
        out += '%dm' % m
    return out


def _describe(body, want):
    """If nick `want` (already toLower'd) is the actor in this log line,
    return a human description of the action, else None."""

    def eq(nick):
        return ircutils.toLower(nick.lstrip('@+%&~')) == want

    # <nick> message
    if body.startswith('<'):
        i = body.find('> ')
        if i != -1 and eq(body[1:i]):
            return 'saying: ' + body[i + 2:]
        return None

    # * nick does something  (/me action)
    if body.startswith('* '):
        parts = body[2:].split(' ', 1)
        if eq(parts[0]):
            return 'doing: * %s' % body[2:]
        return None

    # -nick- notice text
    if body.startswith('-'):
        m = _NOTICE_RE.match(body)
        if m and eq(m.group(1)):
            return '(notice) ' + m.group(2)
        return None

    # *** ... events
    if body.startswith('*** '):
        rest = body[4:]
        actor = rest.split(' ', 1)[0]
        if ' has joined ' in rest:
            if eq(actor):
                return 'joining ' + rest.split(' has joined ', 1)[1]
        elif ' has left ' in rest:
            if eq(actor):
                return 'leaving ' + rest.split(' has left ', 1)[1]
        elif ' has quit IRC' in rest:
            if eq(actor):
                return 'quitting IRC' + rest.split(' has quit IRC', 1)[1]
        elif ' is now known as ' in rest:
            (old, new) = rest.split(' is now known as ', 1)
            new = new.strip()
            if eq(old):
                return 'changing nick to ' + new
            if eq(new):
                return 'showing up (renamed from %s)' % old
        elif ' was kicked by ' in rest:
            (target, by) = rest.split(' was kicked by ', 1)
            if eq(target):
                return 'getting kicked by ' + by
            if eq(by.split(' ', 1)[0]):
                return 'kicking ' + target
        elif ' sets mode: ' in rest:
            if eq(actor):
                return 'setting mode: ' + rest.split(' sets mode: ', 1)[1]
        elif ' changes topic to ' in rest:
            if eq(actor):
                return 'changing the topic'
        elif ' is now away' in rest:
            if eq(actor):
                return 'going away' + rest.split(' is now away', 1)[1]
        elif ' is back' in rest:
            if eq(actor):
                return 'coming back'
        elif ' invited ' in rest:
            if eq(actor):
                return rest
    return None


class Seen(callbacks.Plugin):
    """Logs channel activity and reports when a user was last active.

    seenlog [<channel>] [on|off]   enable/disable logging (alias for the 'seen'
                                   channel capability; no arg shows status)
    seen [<channel>] <nick>        report <nick>'s last action in the channel
    """

    threaded = True       # !seen may read a large log file
    noIgnore = True       # log even users on the ignore list
    echoMessage = True    # also log the bot's own messages, like ChannelLogger

    # ------------------------------------------------------------ enable state
    def _enabled(self, channel):
        """Logging is on iff the channel carries the 'seen' capability, set via
        'chancap seen' (or 'seenlog on'). Defaults to off when unset."""
        if not channel:
            return False
        try:
            return ircdb.channels.getChannel(channel).capabilities.check('seen')
        except KeyError:           # neither 'seen' nor '-seen' present -> off
            return False

    # ------------------------------------------------------------ log writing
    def _seen_path(self, channel):
        base = channel.lstrip('#&+!').lower() or ircutils.toLower(channel)
        fname = utils.file.sanitizeName(base) + '.seen.log'
        return os.path.join(conf.supybot.directories.log.dirize('Seen'), fname)

    def _log(self, irc, channel, fmt, *fmtargs):
        if not irc.isChannel(channel):
            return
        if not self._enabled(channel):
            return
        s = ircutils.stripFormatting(fmt % fmtargs if fmtargs else fmt)
        path = self._seen_path(channel)
        try:
            logdir = os.path.dirname(path)
            if not os.path.exists(logdir):
                os.makedirs(logdir)
            ts = time.strftime(conf.supybot.log.timestampFormat())
            with open(path, 'a', encoding='utf-8') as f:
                f.write('%s  %s' % (ts, s))   # fmt strings already end in \n
        except OSError:
            self.log.exception('Seen: could not write %s', path)

    # ------------------------------------------------------------ event hooks
    def doPrivmsg(self, irc, msg):
        (recipients, text) = msg.args
        nick = msg.nick or irc.nick
        for channel in recipients.split(','):
            if irc.isChannel(channel):
                if ircmsgs.isAction(msg):
                    self._log(irc, channel, '* %s %s\n',
                              nick, ircmsgs.unAction(msg))
                else:
                    self._log(irc, channel, '<%s> %s\n', nick, text)

    def doNotice(self, irc, msg):
        (recipients, text) = msg.args
        for channel in recipients.split(','):
            if irc.isChannel(channel):
                self._log(irc, channel, '-%s- %s\n', msg.nick or irc.nick, text)

    def doJoin(self, irc, msg):
        for channel in msg.args[0].split(','):
            self._log(irc, channel, '*** %s <%s> has joined %s\n',
                      msg.nick, msg.prefix, channel)

    def doPart(self, irc, msg):
        reason = ' (%s)' % msg.args[1] if len(msg.args) > 1 else ''
        for channel in msg.args[0].split(','):
            self._log(irc, channel, '*** %s <%s> has left %s%s\n',
                      msg.nick, msg.prefix, channel, reason)

    def doQuit(self, irc, msg):
        reason = ' (%s)' % msg.args[0] if len(msg.args) == 1 else ''
        for channel in msg.tagged('channels'):
            self._log(irc, channel, '*** %s <%s> has quit IRC%s\n',
                      msg.nick, msg.prefix, reason)

    def doNick(self, irc, msg):
        (oldNick, newNick) = (msg.nick, msg.args[0])
        for channel in msg.tagged('channels'):
            self._log(irc, channel, '*** %s is now known as %s\n',
                      oldNick, newNick)

    def doKick(self, irc, msg):
        if len(msg.args) == 3:
            (channel, target, kickmsg) = msg.args
        else:
            (channel, target) = msg.args
            kickmsg = ''
        if kickmsg:
            self._log(irc, channel, '*** %s was kicked by %s (%s)\n',
                      target, msg.nick, kickmsg)
        else:
            self._log(irc, channel, '*** %s was kicked by %s\n',
                      target, msg.nick)

    def doMode(self, irc, msg):
        channel = msg.args[0]
        if irc.isChannel(channel) and msg.args[1:]:
            self._log(irc, channel, '*** %s sets mode: %s %s\n',
                      msg.nick or msg.prefix, msg.args[1],
                      ' '.join(msg.args[2:]))

    def doTopic(self, irc, msg):
        if len(msg.args) == 1:
            return
        channel = msg.args[0]
        self._log(irc, channel, '*** %s changes topic to "%s"\n',
                  msg.nick, msg.args[1])

    # --------------------------------------------------------------- commands
    def seenlog(self, irc, msg, args, channel, state):
        """[<channel>] [on|off]

        Enable or disable !seen logging for <channel> (defaults to the current
        channel). This is just a friendly alias for the 'seen' channel
        capability, so it is exactly equivalent to: chancap seen / unchancap
        seen. With no argument, reports the current status.
        """
        if state is None:
            irc.reply('seen logging is %s for %s (file: %s).'
                      % ('ON' if self._enabled(channel) else 'OFF', channel,
                         self._seen_path(channel)))
            return
        chan = ircdb.channels.getChannel(channel)
        if state == 'on':
            chan.addCapability('seen')
        else:
            try:
                chan.removeCapability('seen')
            except KeyError:
                pass
        ircdb.channels.setChannel(channel, chan)
        irc.reply('seen logging %s for %s.'
                  % ('ENABLED' if state == 'on' else 'DISABLED', channel))
    seenlog = wrap(seenlog, [('checkCapability', 'admin'), 'channel',
                             optional(('literal', ('on', 'off')))])

    def seen(self, irc, msg, args, channel, nick):
        """[<channel>] <nick>

        Report how long ago <nick> was last active in <channel> (defaults to
        the current channel) and the exact line they were last seen in.
        """
        path = self._seen_path(channel)
        if not os.path.exists(path):
            if self._enabled(channel):
                irc.reply('I have nothing logged yet for %s.' % channel)
            else:
                irc.reply('seen logging is not enabled for %s '
                          '(enable it with: chancap seen).' % channel)
            return
        want = ircutils.toLower(nick)
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except OSError as e:
            irc.error('could not read the seen log: %s' % e)
            return
        for line in reversed(lines):
            parsed = _parse_line(line)
            if parsed is None:
                continue
            (dt, body) = parsed
            if _describe(body, want) is not None:
                shown = body
                if len(shown) > 350:
                    shown = shown[:347] + '...'
                ago = _format_ago((datetime.now() - dt).total_seconds())
                irc.reply('%s was last seen %s ago: %s' % (nick, ago, shown))
                return
        irc.reply("I haven't seen %s in %s." % (nick, channel))
    seen = wrap(seen, ['channel', 'something'])


Class = Seen
