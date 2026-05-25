import supybot.conf as conf


def configure(advanced):
    conf.registerPlugin('Seen', True)


Seen = conf.registerPlugin('Seen')
