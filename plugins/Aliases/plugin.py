import supybot.callbacks as callbacks
import supybot.ircdb as ircdb
import supybot.ircutils as ircutils
from supybot.commands import wrap, first, optional


class Aliases(callbacks.Plugin):
    """Owner shortcuts for common admin tasks.

    !addhost <user> <hostmask>   add a hostmask to a registered user
    """

    threaded = False

    def addhost(self, irc, msg, args, user, hostmask):
        """<user> <hostmask>

        Adds <hostmask> to the registered user <user>. Owner only.
        """
        if not ircdb.checkCapability(msg.prefix, 'owner'):
            irc.errorNoCapability('owner', Raise=True)
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


Class = Aliases
