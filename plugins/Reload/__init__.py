###
# Reload — one-shot batch reloader for the bot's local plugins.
###

import supybot
from supybot import world

__version__ = "1.0"
__author__ = supybot.Author("psg", "psg", "psg@dont.panic")
__contributors__ = {}
__url__ = ""

from . import config
from . import plugin
from importlib import reload
reload(config)
reload(plugin)

Class = plugin.Class
configure = config.configure
