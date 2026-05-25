###
# YouTube plugin for fatbot/Limnoria.
###

"""
YouTube: Snarfs YouTube video URLs and posts metadata via yt-dlp,
followed by a shrunk URL via the ShrinkUrl plugin.
"""

import supybot
from supybot import world

__version__ = "2026.05.08"
__author__ = supybot.Author('fatbot', 'fatbot', 'fatbot@example.com')
__contributors__ = {}
__url__ = ''

from . import config
from . import plugin
from importlib import reload
reload(plugin)

if world.testing:
    from . import test

Class = plugin.Class
configure = config.configure

# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
