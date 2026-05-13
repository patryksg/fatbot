"""
Create: !create <prompt> -> Venice.ai image generation -> catbox.moe upload.
"""

import supybot
from supybot import world

__version__ = "2026.05.12"
__author__ = supybot.authors.unknown
__contributors__ = {}
__url__ = ''

from . import config
from . import plugin
from importlib import reload
reload(plugin)

Class = plugin.Class
configure = config.configure
