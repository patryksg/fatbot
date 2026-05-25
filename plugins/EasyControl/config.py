import supybot.conf as conf
import supybot.registry as registry


def configure(advanced):
    conf.registerPlugin('EasyControl', True)


EasyControl = conf.registerPlugin('EasyControl')

conf.registerChannelValue(EasyControl, 'modes',
    registry.String('', """Channel modes the bot auto-enforces whenever it has
    ops (e.g. +npst). Empty disables enforcement for the channel. Set it with:
    chanmode [<channel>] <modes>"""))
