import supybot.conf as conf
import supybot.registry as registry


def configure(advanced):
    pass


Wikibear = conf.registerPlugin('Wikibear')

conf.registerChannelValue(Wikibear, 'enabled',
    registry.Boolean(False,
        '!wikibear is only available in channels where this is True.'))

conf.registerGlobalValue(Wikibear, 'timeoutSec',
    registry.PositiveInteger(120,
        'How long to wait for the claude CLI to produce a factoid.'))
