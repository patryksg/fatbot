"""Auto-enforce channel modes when fatbot has ops."""
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
