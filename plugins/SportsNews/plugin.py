###
# Copyright (c) 2026, SportsNews Contributors
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author nor the names of its contributors may be
#     used to endorse or promote products derived from this software without
#     specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
###

import json

from supybot import callbacks, utils, world, ircutils
from supybot.commands import *


LEAGUE_PATHS = {
    # US major leagues
    "nfl": "football/nfl",
    "nba": "basketball/nba",
    "wnba": "basketball/wnba",
    "mlb": "baseball/mlb",
    "nhl": "hockey/nhl",
    "mls": "soccer/usa.1",
    # College
    "ncaaf": "football/college-football",
    "ncaab": "basketball/mens-college-basketball",
    # Soccer
    "epl": "soccer/eng.1",
    "laliga": "soccer/esp.1",
    "seriea": "soccer/ita.1",
    "bundesliga": "soccer/ger.1",
    "ligue1": "soccer/fra.1",
    "ucl": "soccer/uefa.champions",
    # Other
    "ufc": "mma/ufc",
    "f1": "racing/f1",
    "pga": "golf/pga",
}

NEWS_URL_TEMPLATE = "https://site.api.espn.com/apis/site/v2/sports/{path}/news"
MAX_ARTICLES = 3


class SportsNews(callbacks.Plugin):
    """A plugin that fetches the latest sports news headlines from ESPN."""
    threaded = True

    @wrap(["somethingWithoutSpaces"])
    def sports(self, irc, msg, args, league):
        """<league>

        Fetch the latest headlines from ESPN for the given league.
        Supported: nfl, nba, wnba, mlb, nhl, mls, ncaaf, ncaab, epl, laliga,
        seriea, bundesliga, ligue1, ucl, ufc, f1, pga.
        """
        league = league.lower()
        path = LEAGUE_PATHS.get(league)
        if path is None:
            irc.error(
                "Unknown league. Supported: {}".format(
                    ", ".join(sorted(LEAGUE_PATHS))
                )
            )
            return

        url = NEWS_URL_TEMPLATE.format(path=path)
        try:
            raw = utils.web.getUrl(url)
            data = json.loads(raw)
        except Exception:
            self.log.exception(
                "Failed to fetch news for %s from %s", league, url
            )
            irc.error(
                "Sorry, I couldn't fetch the news for {} right now. "
                "Please try again later.".format(league)
            )
            return

        articles = (data.get("articles") or [])[:MAX_ARTICLES]
        if not articles:
            irc.reply("No recent news found for {}.".format(league))
            return
        for article in articles:
            headline = article.get("headline") or ""
            if not headline:
                continue
            link = (((article.get("links") or {}).get("web") or {})
                    .get("href") or "")
            if link:
                irc.reply("{} - {}".format(headline, self._shorten(link)))
            else:
                irc.reply(headline)

    def _shorten(self, url):
        """Run a URL through the bot's ShrinkUrl plugin (t.ly chain, blue-
        colored), matching the rest of the bot's link style. Uses world.ircs to
        reach the real Irc object -- the command proxy has no getCallback().
        Falls back to a blue-colored long URL if ShrinkUrl is unavailable."""
        try:
            for irc in world.ircs:
                shrink = irc.getCallback("ShrinkUrl")
                if shrink is not None:
                    try:
                        return shrink._getTlyUrl(url)
                    except Exception:
                        self.log.info(
                            "SportsNews: ShrinkUrl shorten failed for %s", url
                        )
        except Exception:
            pass
        return ircutils.mircColor(url, "12")
