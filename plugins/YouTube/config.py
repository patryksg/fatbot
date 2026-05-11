###
# YouTube plugin config.
###

import supybot.conf as conf
import supybot.registry as registry
from supybot.i18n import PluginInternationalization
_ = PluginInternationalization('YouTube')


def configure(advanced):
    from supybot.questions import yn
    conf.registerPlugin('YouTube', True)
    if yn(_("""This plugin offers a snarfer that posts video metadata for
             YouTube URLs (title, uploader, duration, views, upload date,
             hashtags) using yt-dlp. Would you like the snarfer to be
             enabled?"""), default=True):
        conf.supybot.plugins.YouTube.snarfer.setValue(True)


YouTube = conf.registerPlugin('YouTube')

conf.registerChannelValue(YouTube, 'snarfer',
    registry.Boolean(True, _("""Determines whether the YouTube snarfer is
    enabled. When a YouTube video URL is posted, the bot fetches metadata
    via yt-dlp and posts it to the channel.""")))

conf.registerChannelValue(YouTube, 'bold',
    registry.Boolean(True, _("""Whether the video title is shown bold.""")))

# White " ▶ " on red background, then black " YouTube " on white background.
# Uses mIRC color codes (\x03<fg>,<bg> ... \x03) — works in mIRC, HexChat,
# WeeChat, KVIrc, irssi (with /set show_colors) etc.
_DEFAULT_PREFIX = '\x0300,04 ▶ \x0301,00 YouTube \x03 '

conf.registerChannelValue(YouTube, 'prefix',
    registry.String(_DEFAULT_PREFIX, _("""Prefix prepended to the info
    line. Defaults to a white play-arrow + 'YouTube' on a red background
    using mIRC color codes. Set to '' to disable.""")))

conf.registerChannelValue(YouTube, 'maxHashtags',
    registry.NonNegativeInteger(4, _("""Maximum number of hashtags to
    derive from the video's tag list. Set to 0 to disable hashtags.""")))

conf.registerChannelValue(YouTube, 'timeout',
    registry.PositiveInteger(15, _("""Timeout (seconds) for the yt-dlp
    metadata extraction subprocess.""")))

conf.registerChannelValue(YouTube, 'cookiesFile',
    registry.String('/home/fatbot/runbot/youtube-cookies.txt', _("""Path to
    a Netscape-format cookies.txt for yt-dlp. Required when YouTube blocks
    anonymous metadata requests with 'Sign in to confirm you're not a bot'.
    Empty string disables cookies.""")))

conf.registerChannelValue(YouTube, 'shrink',
    registry.Boolean(True, _("""Whether to also post a shrunk URL via the
    ShrinkUrl plugin after the info line. Requires the ShrinkUrl plugin to
    be loaded.""")))

conf.registerChannelValue(YouTube, 'shrinkBold',
    registry.Boolean(True, _("""Whether the shrunk URL is shown bold.""")))

conf.registerChannelValue(YouTube, 'shrinkShowDomain',
    registry.Boolean(False, _("""Whether to append " (at <domain>)" to the
    shrunk URL line, like ShrinkUrl's own snarfer does.""")))

# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
