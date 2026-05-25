import supybot
from supybot import world

__version__ = "0.1"
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
