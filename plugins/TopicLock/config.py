import supybot.conf as conf
import supybot.registry as registry


def configure(advanced):
    conf.registerPlugin('TopicLock', True)


TopicLock = conf.registerPlugin('TopicLock')
