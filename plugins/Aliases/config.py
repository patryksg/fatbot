import supybot.conf as conf
import supybot.registry as registry


def configure(advanced):
    conf.registerPlugin('Aliases', True)


Aliases = conf.registerPlugin('Aliases')
