"""Wikibear: !wikibear shares an absurd Wikipedia factoid as wiki bear."""

import supybot
from supybot import world

__version__ = "0.1"
__author__ = supybot.Author("fatbot", "fatbot", "noreply@example.com")
__contributors__ = {}
__url__ = ""

from . import config
from . import plugin
from importlib import reload
reload(config)
reload(plugin)

if world.testing:
    from . import test

Class = plugin.Class
configure = config.configure
