###
# EasyControl — one home for the bot's user/channel administration.
#
# Consolidates what used to be spread across InfoToggle, Aliases, ChanModes and
# TopicLock (and the duplicated cap/chancap commands in Create):
#
#   capabilities : cap, uncap, remcap, chancap, unchancap
#   users        : adduser, deluser, addhost
#   feature flags: info (URL titles), chanmode (mode enforcement)
#   behaviour    : auto-enforces channel modes when opped; topic lock/guard
#
# All commands are owner-gated.
###

import secrets
import string

import supybot.conf as conf
import supybot.ircdb as ircdb
import supybot.ircmsgs as ircmsgs
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
from supybot.commands import wrap, optional, first, additional


def _random_password(length=20):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


def _wildcard_hostmask(full_hostmask):
    """nick!user@host -> *!*@*.domain or *!*@host (no wildcarding for IPs)."""
    try:
        (nick, user, host) = ircutils.splitHostmask(full_hostmask)
    except Exception:
        return full_hostmask
    parts = host.split('.')
    if len(parts) >= 3 and not host[-1].isdigit():
        wild_host = '*.' + '.'.join(parts[1:])
    else:
        wild_host = host
    return '*!*@' + wild_host


def _set_channel_cfg(plugin_name, key_name, channel, value):
    plugin_conf = getattr(conf.supybot.plugins, plugin_name)
    key_conf = getattr(plugin_conf, key_name)
    key_conf.get(channel).setValue(value)


def _resolve_channel(msg, channel):
    if channel:
        return channel
    target = msg.args[0] if msg.args else None
    if target and target.startswith(("#", "&", "+", "!")):
        return target
    return None


def _find_user(irc, name):
    """Return ircdb user by username or current IRC nick, or raise KeyError."""
    try:
        return ircdb.users.getUser(name)
    except KeyError:
        pass
    try:
        hostmask = irc.state.nickToHostmask(name)
        return ircdb.users.getUser(hostmask)
    except KeyError:
        raise KeyError(name)


def _parse_modes(s):
    out = []
    sign = '+'
    for ch in s:
        if ch in '+-':
            sign = ch
        elif ch.isalpha():
            out.append((sign, ch))
    return out


class EasyControl(callbacks.Plugin):
    """User & channel administration: !cap, !uncap, !remcap, !chancap,
    !unchancap, !adduser, !deluser, !addhost, !info, !chanmode, !topic.
    Also auto-enforces channel modes when opped and guards locked topics."""

    def __init__(self, irc):
        super().__init__(irc)
        self._locked = {}   # channel.lower() -> snapshotted topic

    def _check_owner(self, irc, msg):
        if not ircdb.checkCapability(msg.prefix, "owner"):
            irc.errorNoCapability("owner")
            return False
        return True

    # ============================================ channel-mode enforcement ===
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
        self._enforce(irc, msg.args[1])

    def doMode(self, irc, msg):
        # Re-assert when a mode change happens (covers regaining ops)
        if not msg.args:
            return
        self._enforce(irc, msg.args[0])

    # ======================================================= topic guard =====
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
        if not self._check_owner(irc, msg):
            return
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

    # ================================================== channel feature flags =
    def info(self, irc, msg, args, channel, toggle):
        """[<#channel>] on|off

        Enable or disable URL title fetching + link shortening for a channel.
        """
        if not self._check_owner(irc, msg):
            return
        channel = _resolve_channel(msg, channel)
        if not channel:
            irc.error("Could not determine channel.")
            return
        if toggle == "on":
            _set_channel_cfg("Title", "enable", channel, True)
            _set_channel_cfg("Title", "useShrinkUrl", channel, True)
            _set_channel_cfg("ShrinkUrl", "shrinkSnarfer", channel, False)
            irc.reply("URL titles + shortening: ON for " + channel)
        else:
            _set_channel_cfg("Title", "enable", channel, False)
            _set_channel_cfg("Title", "useShrinkUrl", channel, False)
            irc.reply("URL titles + shortening: OFF for " + channel)
    info = wrap(info, [optional("channel"), ("literal", ("on", "off"))])

    def chanmode(self, irc, msg, args, channel, modes):
        """[<#channel>] <modes>

        Set the channel modes the bot auto-enforces when opped (e.g. +pnst).
        Pass "" to disable enforcement for the channel.
        """
        if not self._check_owner(irc, msg):
            return
        channel = _resolve_channel(msg, channel)
        if not channel:
            irc.error("Could not determine channel.")
            return
        self.setRegistryValue('modes', modes, channel)
        if modes:
            self._enforce(irc, channel)
            irc.reply("ChanModes for " + channel + " set to: " + modes)
        else:
            irc.reply("ChanModes enforcement disabled for " + channel)
    chanmode = wrap(chanmode, [optional("channel"), "text"])

    # ====================================================== user administration
    def adduser(self, irc, msg, args, nick):
        """<nick>

        Add a new bot user for <nick> using their current hostmask and a
        random password (sent to them via NOTICE).
        """
        if not self._check_owner(irc, msg):
            return
        try:
            full_hostmask = irc.state.nickToHostmask(nick)
        except KeyError:
            irc.error("Nick '" + nick + "' not found in any joined channel.")
            return
        mask = _wildcard_hostmask(full_hostmask)
        try:
            ircdb.users.getUserId(nick)
            irc.error("A user named '" + nick + "' already exists.")
            return
        except KeyError:
            pass
        password = _random_password()
        user = ircdb.users.newUser()
        user.name = nick
        user.setPassword(password)
        user.addHostmask(mask)
        try:
            ircdb.users.setUser(user)
        except ircdb.DuplicateHostmask:
            ircdb.users.delUser(user.id)
            irc.error("Hostmask " + mask + " already registered to another user.")
            return
        irc.queueMsg(ircmsgs.notice(
            nick,
            "You have been registered with the bot. Your password is: " + password
            + " (transmitted via IRC NOTICE; rotate it after first login).",
        ))
        irc.reply("User '" + nick + "' added with hostmask " + mask
                  + ". Password sent to " + nick + " via NOTICE.")
    adduser = wrap(adduser, ["nick"])

    def deluser(self, irc, msg, args, name):
        """<nick|username>

        Delete the bot user with the given username or current IRC nick.
        """
        if not self._check_owner(irc, msg):
            return
        try:
            u = _find_user(irc, name)
        except KeyError:
            irc.error("No user found for '" + name + "'.")
            return
        uname = u.name
        ircdb.users.delUser(u.id)
        irc.reply("User '" + uname + "' deleted.")
    deluser = wrap(deluser, ["something"])

    def addhost(self, irc, msg, args, user, hostmask):
        """<user> <hostmask>

        Add <hostmask> to the registered user <user>.
        """
        if not self._check_owner(irc, msg):
            return
        if not ircutils.isUserHostmask(hostmask):
            irc.errorInvalid('hostmask', hostmask, Raise=True)
        try:
            otherId = ircdb.users.getUserId(hostmask)
            if otherId != user.id:
                irc.error("That hostmask is already registered to %s."
                          % ircdb.users.getUser(otherId).name, Raise=True)
        except KeyError:
            pass
        try:
            user.addHostmask(hostmask)
        except ValueError as e:
            irc.error(str(e), Raise=True)
        try:
            ircdb.users.setUser(user)
        except ircdb.DuplicateHostmask as e:
            user.removeHostmask(hostmask)
            irc.error("That hostmask is already registered to %s." % e.args[0],
                      Raise=True)
        except ValueError as e:
            irc.error(str(e), Raise=True)
        irc.replySuccess()
    addhost = wrap(addhost, [first('otherUser', 'user'), 'something'])

    # ===================================================== capability management
    def _change_user_cap(self, irc, msg, name, capability, add):
        try:
            u = _find_user(irc, name)
        except KeyError:
            irc.error("No user found for '" + name + "'.")
            return
        if ',' not in capability:
            channel = _resolve_channel(msg, None)
            if channel:
                capability = ircdb.makeChannelCapability(channel, capability)
        if add:
            u.addCapability(capability)
            ircdb.users.setUser(u)
            irc.reply("Added capability '" + capability + "' to user '" + u.name + "'.")
        else:
            try:
                u.removeCapability(capability)
            except KeyError:
                irc.error("User '" + u.name + "' does not have capability '"
                          + capability + "'.")
                return
            ircdb.users.setUser(u)
            irc.reply("Removed capability '" + capability + "' from user '" + u.name + "'.")

    def cap(self, irc, msg, args, name, capability):
        """<nick|username> [<capability>]

        Add <capability> to a user (default: generative). A capability without a
        channel prefix (e.g. claude, op, generative) is scoped to the current
        channel; pass a fully-qualified one (e.g. #chan,op) for another channel.
        """
        if not self._check_owner(irc, msg):
            return
        self._change_user_cap(irc, msg, name, capability or "generative", add=True)
    cap = wrap(cap, ["something", optional("somethingWithoutSpaces")])

    def uncap(self, irc, msg, args, name, capability):
        """<nick|username> [<capability>]

        Remove <capability> from a user (default: generative). Bare capabilities
        are scoped to the current channel, mirroring !cap.
        """
        if not self._check_owner(irc, msg):
            return
        self._change_user_cap(irc, msg, name, capability or "generative", add=False)
    uncap = wrap(uncap, ["something", optional("somethingWithoutSpaces")])

    # remcap is a synonym for uncap (kept for muscle memory)
    def remcap(self, irc, msg, args, name, capability):
        """<nick|username> [<capability>]

        Remove <capability> from a user (default: generative). Synonym for !uncap.
        """
        if not self._check_owner(irc, msg):
            return
        self._change_user_cap(irc, msg, name, capability or "generative", add=False)
    remcap = wrap(remcap, ["something", optional("somethingWithoutSpaces")])

    def chancap(self, irc, msg, args, channel, capability):
        """[<#channel>] <capability>

        Enable feature <capability> on <channel> by adding it to the channel's
        capabilities (e.g. generative, seen). Defaults to the current channel.
        """
        if not self._check_owner(irc, msg):
            return
        chan = ircdb.channels.getChannel(channel)
        chan.addCapability(capability)
        ircdb.channels.setChannel(channel, chan)
        irc.reply("Enabled '" + capability + "' on " + channel + ".")
    chancap = wrap(chancap, ["channel", "somethingWithoutSpaces"])

    def unchancap(self, irc, msg, args, channel, capability):
        """[<#channel>] <capability>

        Disable feature <capability> on <channel> by removing it from the
        channel's capabilities. Removes both the positive and negative forms
        so the capability is fully cleared. Defaults to the current channel.
        """
        if not self._check_owner(irc, msg):
            return
        bare = capability.lstrip('-')
        chan = ircdb.channels.getChannel(channel)
        for cap in (bare, '-' + bare):
            try:
                chan.removeCapability(cap)
            except KeyError:
                pass
        ircdb.channels.setChannel(channel, chan)
        irc.reply("Disabled '" + bare + "' on " + channel + ".")
    unchancap = wrap(unchancap, ["channel", "somethingWithoutSpaces"])


Class = EasyControl
