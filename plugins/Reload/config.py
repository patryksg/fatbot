###
# Reload plugin configuration.
###

import supybot.conf as conf
import supybot.registry as registry


def configure(advanced):
    from supybot.questions import expect, anything, something, yn
    conf.registerPlugin("Reload", True)


Reload = conf.registerPlugin("Reload")

# The local plugins that !rl reloads, in order. Edit at runtime with e.g.
#   config plugins.Reload.plugins Ash Claude Create ...
conf.registerGlobalValue(Reload, "plugins",
    registry.SpaceSeparatedListOfStrings(
        ["Ash", "Claude", "Create", "EasyControl", "NuWeather", "Relay",
         "Repo", "Seen", "ShrinkUrl", "Title", "Wikibear", "YouTube"],
        """List of local plugins that the !rl command reloads, in order."""))
