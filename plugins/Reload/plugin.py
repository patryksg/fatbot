###
# Reload — one-shot batch reloader for the bot's local plugins.
#
# Stock Limnoria's Alias maps an alias to a single command, and `reload` takes
# one plugin at a time, so there's no built-in way to reload everything after a
# round of edits. This adds `!rl`, which reloads every plugin named in
# supybot.plugins.Reload.plugins (in order) and reports the result on one line.
#
# It deliberately refuses to reload itself — you can't hot-swap the plugin
# whose code is mid-execution — mirroring Owner.reload's self-guard.
###

import gc
import sys

import supybot.plugin as plugin
import supybot.ircdb as ircdb
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
from supybot.commands import wrap


def _reload_one(irc, name):
    """Replicate Owner.reload for a single plugin. Returns None on success or
    a short error string on failure. Never raises."""
    try:
        cbs = irc.removeCallback(name)
        if not cbs:
            return "not loaded"
        module = sys.modules[cbs[0].__module__]
        x = module.reload() if hasattr(module, "reload") else None
        try:
            module = plugin.loadPluginModule(name)
            if hasattr(module, "reload") and x is not None:
                module.reload(x)
            if hasattr(module, "config"):
                from importlib import reload as _reload
                _reload(module.config)
            for cb in cbs:
                cb.die()
                del cb
            gc.collect()  # make sure the old callback is actually collected
            plugin.loadPluginClass(irc, module)
            return None
        except ImportError:
            # Put the old callbacks back so we don't lose the plugin entirely.
            for cb in cbs:
                irc.addCallback(cb)
            return "no such module"
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)


class Reload(callbacks.Plugin):
    """Batch-reload the bot's local plugins with a single command: !rl."""

    def rl(self, irc, msg, args):
        """takes no arguments

        Reloads every local plugin listed in supybot.plugins.Reload.plugins,
        in order, and reports which succeeded and which failed. Owner-only.
        """
        if not ircdb.checkCapability(msg.prefix, "owner"):
            irc.errorNoCapability("owner")
            return
        ok, failed = [], []
        for name in self.registryValue("plugins"):
            if ircutils.strEqual(name, self.name()):
                continue  # can't reload the plugin running this command
            err = _reload_one(irc, name)
            if err is None:
                ok.append(name)
            else:
                failed.append("%s (%s)" % (name, err))
        parts = []
        if ok:
            parts.append("reloaded %d: %s" % (len(ok), ", ".join(ok)))
        if failed:
            parts.append("FAILED %d: %s" % (len(failed), "; ".join(failed)))
        irc.reply(" | ".join(parts) if parts else "nothing to reload")
    rl = wrap(rl)


Class = Reload
