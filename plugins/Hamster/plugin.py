import random
import time
import supybot.callbacks as callbacks
import supybot.ircmsgs as ircmsgs
import supybot.schedule as schedule
import supybot.world as world

MSG = "hamsters don't MAKE errors"
CHANNEL = "#fatkids"
MIN_DELAY = 300
MAX_DELAY = 21600

class Hamster(callbacks.Plugin):
    """Randomly says hamsters don't MAKE errors in #fatkids."""

    def __init__(self, irc):
        super().__init__(irc)
        self._schedule()

    def _schedule(self):
        delay = random.randint(MIN_DELAY, MAX_DELAY)
        schedule.addEvent(self._fire, time.time() + delay, name="hamster")

    def _fire(self):
        try:
            for irc in world.ircs:
                if CHANNEL in irc.state.channels:
                    irc.queueMsg(ircmsgs.privmsg(CHANNEL, MSG))
                    break
        finally:
            self._schedule()

    def die(self):
        try:
            schedule.removeEvent("hamster")
        except KeyError:
            pass
        super().die()

Class = Hamster
