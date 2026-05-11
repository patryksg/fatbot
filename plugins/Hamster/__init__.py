"""Randomly posts in #yourchannel."""
import supybot
import supybot.world as world
from . import config
from . import plugin
from importlib import reload
reload(plugin)
if world.testing:
    from . import test
Class = plugin.Class
configure = config.configure
