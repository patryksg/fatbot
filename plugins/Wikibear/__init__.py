"""Wikibear: !wikibear shares an absurd Wikipedia factoid as wiki bear."""

import supybot
from supybot import world

__version__ = "2026.05.26"
__author__ = supybot.Author("psg", "psg", "noreply@example.com")
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
