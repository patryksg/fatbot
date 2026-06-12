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
    # 'opus', 'normal' and 'gem' are legacy values kept so old per-channel
    # settings in fatbot.conf still load; they all behave like 'haiku'.
    validStrings = ('haiku', 'fable', 'opus', 'normal', 'gem')

conf.registerChannelValue(Claude, 'mode',
    ClaudeMode('haiku',
        'Active model for this channel. '
        '"haiku" = Claude Haiku, cheap default (also any legacy value: '
        'opus/normal/gem). '
        '"fable" = highest model at max effort (expensive). '
        'Switched in-channel via !haiku (or !claude) / !fable.'))

conf.registerGlobalValue(Claude, 'haikuModel',
    registry.String('claude-haiku-4-5-20251001',
        'Model used for regular (haiku-mode) replies. Change live via '
        '`config plugins.Claude.haikuModel <model>` — no reload needed.'))

conf.registerGlobalValue(Claude, 'fableModel',
    registry.String('claude-fable-5',
        'Model used in fable mode (!fable). Change live via '
        '`config plugins.Claude.fableModel <model>` — no reload needed.'))

conf.registerGlobalValue(Claude, 'fableEffort',
    registry.String('max',
        'Effort level passed to the Claude CLI in fable mode '
        '(low, medium, high, xhigh, max). Empty = omit the flag.'))
