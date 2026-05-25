import supybot.conf as conf
import supybot.registry as registry

def configure(advanced):
    pass

Claude = conf.registerPlugin('Claude')

conf.registerChannelValue(Claude, 'channelEnabled',
    registry.Boolean(False, 'Whether !claude is enabled in this channel.'))

conf.registerChannelValue(Claude, 'smartMode',
    registry.Boolean(False,
        'DEPRECATED. Replaced by `mode`. Kept only for backward compat.'))

class ClaudeMode(registry.OnlySomeStrings):
    validStrings = ('haiku', 'opus', 'gem')

conf.registerChannelValue(Claude, 'mode',
    ClaudeMode('haiku',
        'Active model for this channel. '
        '"haiku" = Claude Haiku, up to 6 lines. '
        '"opus"  = Claude Opus up to 6 lines (smart). '
        '"gem"   = Gemini 2.5 Flash, up to 6 lines. '
        'Switched in-channel via !claude / !smart / !gem.'))

conf.registerChannelValue(Claude, 'geminiFallback',
    registry.Boolean(True,
        'When True and current mode is haiku/opus, on a Claude rate-limit / '
        'quota error the channel auto-switches to "gem" mode and the question '
        'is answered by Gemini. The auto-fallback answer is suffixed with "(gem)".'))
