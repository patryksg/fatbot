import supybot.conf as conf
import supybot.registry as registry

def configure(advanced):
    pass

Claude = conf.registerPlugin('Claude')

conf.registerChannelValue(Claude, 'channelEnabled',
    registry.Boolean(False, 'Whether !claude is enabled in this channel.'))
