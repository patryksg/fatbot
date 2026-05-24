import secrets
import string
import supybot.conf as conf
import supybot.ircdb as ircdb
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
import supybot.ircmsgs as ircmsgs
from supybot.commands import wrap, optional


def _random_password(length=20):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


def _wildcard_hostmask(full_hostmask):
    """nick!user@host → *!*@*.domain or *!*@host (no wildcarding for IPs)."""
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


class InfoToggle(callbacks.Plugin):
    """Admin shortcuts: !info, !ai, !chanmode, !chancap, !unchancap, !adduser, !deluser, !cap, !remcap."""

    def _check_owner(self, irc, msg):
        if not ircdb.checkCapability(msg.prefix, "owner"):
            irc.errorNoCapability("owner")
            return False
        return True

    # ------------------------------------------------------------------ !info
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

    # ------------------------------------------------------------------- !ai
    def ai(self, irc, msg, args, channel, toggle):
        """[<#channel>] on|off

        Grant or revoke the 'claude' channel capability for all users.
        """
        if not self._check_owner(irc, msg):
            return
        channel = _resolve_channel(msg, channel)
        if not channel:
            irc.error("Could not determine channel.")
            return
        if toggle == "on":
            _set_channel_cfg("Claude", "channelEnabled", channel, True)
            irc.reply("AI (!claude) enabled for " + channel)
        else:
            _set_channel_cfg("Claude", "channelEnabled", channel, False)
            irc.reply("AI (!claude) disabled for " + channel)

    ai = wrap(ai, [optional("channel"), ("literal", ("on", "off"))])

    # -------------------------------------------------------------- !chanmode
    def chanmode(self, irc, msg, args, channel, modes):
        """[<#channel>] <modes>

        Set the auto-enforced channel modes for a channel (e.g. +pnst).
        """
        if not self._check_owner(irc, msg):
            return
        channel = _resolve_channel(msg, channel)
        if not channel:
            irc.error("Could not determine channel.")
            return
        _set_channel_cfg("ChanModes", "modes", channel, modes)
        if modes:
            irc.reply("ChanModes for " + channel + " set to: " + modes)
        else:
            irc.reply("ChanModes enforcement disabled for " + channel)

    chanmode = wrap(chanmode, [optional("channel"), "text"])

    # -------------------------------------------------------------- !adduser
    def adduser(self, irc, msg, args, nick):
        """<nick>

        Add a new bot user for <nick> using their current hostmask and a
        random 8-character password.
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
        except ircdb.DuplicateHostmask as e:
            ircdb.users.delUser(user.id)
            irc.error("Hostmask " + mask + " already registered to another user.")
            return
        irc.queueMsg(ircmsgs.notice(
            nick,
            "You have been registered with the bot. Your password is: " + password
            + " (transmitted via IRC NOTICE; rotate it after first login).",
        ))
        irc.reply("User '" + nick + "' added with hostmask " + mask + ". Password sent to " + nick + " via NOTICE.")

    adduser = wrap(adduser, ["nick"])

    # -------------------------------------------------------------- !deluser
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

    # ------------------------------------------------------------------ !cap
    def cap(self, irc, msg, args, name, capability):
        """<nick|username> <capability>

        Add <capability> to a user. If given without a channel prefix (e.g.
        claude, op), it is scoped to the current channel.
        """
        if not self._check_owner(irc, msg):
            return
        try:
            u = _find_user(irc, name)
        except KeyError:
            irc.error("No user found for '" + name + "'.")
            return
        if ',' not in capability:
            channel = _resolve_channel(msg, None)
            if channel:
                capability = ircdb.makeChannelCapability(channel, capability)
        u.addCapability(capability)
        ircdb.users.setUser(u)
        irc.reply("Added capability '" + capability + "' to user '" + u.name + "'.")

    cap = wrap(cap, ["something", "something"])

    # --------------------------------------------------------------- !remcap
    def remcap(self, irc, msg, args, name, capability):
        """<nick|username> <capability>

        Remove <capability> from a user.
        """
        if not self._check_owner(irc, msg):
            return
        try:
            u = _find_user(irc, name)
        except KeyError:
            irc.error("No user found for '" + name + "'.")
            return
        if ',' not in capability:
            channel = _resolve_channel(msg, None)
            if channel:
                capability = ircdb.makeChannelCapability(channel, capability)
        try:
            u.removeCapability(capability)
        except KeyError:
            irc.error("User '" + u.name + "' does not have capability '" + capability + "'.")
            return
        ircdb.users.setUser(u)
        irc.reply("Removed capability '" + capability + "' from user '" + u.name + "'.")

    remcap = wrap(remcap, ["something", "something"])

    # -------------------------------------------------------------- !chancap
    def chancap(self, irc, msg, args, channel, capability):
        """[<#channel>] <capability>

        Enable feature <capability> on <channel> by adding it to the channel's
        capabilities (e.g. 'generative'). Defaults to the current channel.
        """
        if not self._check_owner(irc, msg):
            return
        channel = _resolve_channel(msg, channel)
        if not channel:
            irc.error("Could not determine channel.")
            return
        chan = ircdb.channels.getChannel(channel)
        chan.addCapability(capability)
        ircdb.channels.setChannel(channel, chan)
        irc.reply("Enabled '" + capability + "' on " + channel + ".")

    chancap = wrap(chancap, [optional("channel"), "somethingWithoutSpaces"])

    # ------------------------------------------------------------ !unchancap
    def unchancap(self, irc, msg, args, channel, capability):
        """[<#channel>] <capability>

        Disable feature <capability> on <channel> by removing it from the
        channel's capabilities. Defaults to the current channel.
        """
        if not self._check_owner(irc, msg):
            return
        channel = _resolve_channel(msg, channel)
        if not channel:
            irc.error("Could not determine channel.")
            return
        chan = ircdb.channels.getChannel(channel)
        try:
            chan.removeCapability(capability)
        except KeyError:
            pass
        ircdb.channels.setChannel(channel, chan)
        irc.reply("Disabled '" + capability + "' on " + channel + ".")

    unchancap = wrap(unchancap, [optional("channel"), "somethingWithoutSpaces"])


Class = InfoToggle
