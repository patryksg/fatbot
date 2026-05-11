import supybot.conf as conf
import supybot.registry as registry


def configure(advanced):
    pass


ChanModes = conf.registerPlugin('ChanModes')
conf.registerChannelValue(ChanModes, 'modes',
    registry.String('', """Modes to enforce on this channel when the bot has ops.
    Format like '+pnst' or '+pnst-ik'. Empty disables enforcement."""))
