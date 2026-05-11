###
# YouTube plugin: snarfer that posts video metadata via yt-dlp and a
# shrunk URL via the ShrinkUrl plugin.
#
# Designed to coexist with ShrinkUrl by handling both messages itself.
# Configure ShrinkUrl.nonSnarfingRegexp to skip YouTube URLs so the
# shrunk-URL line only fires once and in the right order.
###

import re
import json
import subprocess
import threading

import supybot.conf as conf
import supybot.utils as utils
import supybot.ircmsgs as ircmsgs
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
from supybot.i18n import PluginInternationalization
_ = PluginInternationalization('YouTube')


# Match YouTube *video* URLs only — channels/playlists are ignored so the
# regular ShrinkUrl snarfer keeps handling them.
YOUTUBE_RE = re.compile(
    r'\bhttps?://(?:[a-z0-9-]+\.)?'
    r'(?:youtu\.be/[\w-]{11}'
    r'|youtube\.com/(?:watch\?(?:[\w%=&.+-]+&)*v=[\w-]{11}'
    r'|shorts/[\w-]{11}'
    r'|live/[\w-]{11}'
    r'|embed/[\w-]{11}))'
    r'(?:[?&#][^\s]*)?',
    re.IGNORECASE,
)

YT_DLP = '/usr/bin/yt-dlp'


def _human_views(n):
    if n is None:
        return None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    for unit, divisor in (('B', 10 ** 9), ('M', 10 ** 6), ('K', 10 ** 3)):
        if n >= divisor:
            v = n / divisor
            s = ('%.1f' % v).rstrip('0').rstrip('.')
            return s + unit
    return str(n)


def _format_date(ymd):
    if not ymd or len(ymd) != 8 or not ymd.isdigit():
        return None
    return '%s-%s-%s' % (ymd[0:4], ymd[4:6], ymd[6:8])


def _format_duration(info):
    if info.get('live_status') == 'is_live':
        return 'LIVE'
    s = info.get('duration_string')
    if s:
        return s
    d = info.get('duration')
    if d is None:
        return None
    try:
        d = int(d)
    except (TypeError, ValueError):
        return None
    if d >= 3600:
        return '%d:%02d:%02d' % (d // 3600, (d % 3600) // 60, d % 60)
    return '%d:%02d' % (d // 60, d % 60)


def _hashtagify(tag):
    h = re.sub(r'\W+', '', tag, flags=re.UNICODE).lower()
    return ('#' + h) if h else None


class YouTube(callbacks.PluginRegexp):
    """Snarfer for YouTube video URLs."""

    regexps = ['youtubeSnarfer']
    flags = re.IGNORECASE

    def __init__(self, irc):
        self.__parent = super(YouTube, self)
        self.__parent.__init__(irc)

    def _fetchInfo(self, url, timeout, cookies):
        cmd = [YT_DLP, '--skip-download', '--no-playlist',
               '--no-warnings', '--socket-timeout', '8',
               '--ignore-no-formats-error']
        if cookies:
            cmd += ['--cookies', cookies]
        cmd += ['--dump-json', '--', url]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout, text=True,
            )
        except subprocess.TimeoutExpired:
            self.log.info('YouTube: yt-dlp timeout for %s', url)
            return None
        except FileNotFoundError:
            self.log.error('YouTube: %s not found', YT_DLP)
            return None
        except Exception:
            self.log.exception('YouTube: yt-dlp invocation failed')
            return None

        if proc.returncode != 0:
            self.log.info(
                'YouTube: yt-dlp returned %i for %s: %s',
                proc.returncode, url,
                (proc.stderr or '').strip()[:200],
            )
            return None
        lines = (proc.stdout or '').strip().splitlines()
        if not lines:
            return None
        try:
            return json.loads(lines[0])
        except ValueError:
            self.log.exception('YouTube: failed to parse yt-dlp JSON')
            return None

    def _formatInfo(self, info, channel, network):
        bits = []

        title = (info.get('title') or '').strip()
        if title:
            if self.registryValue('bold', channel, network):
                title = ircutils.bold(title)
            bits.append(title)

        uploader = (info.get('uploader') or info.get('channel') or '').strip()
        if uploader:
            bits.append(uploader)

        dur = _format_duration(info)
        if dur:
            bits.append(dur)

        views = _human_views(info.get('view_count'))
        if views:
            bits.append('%s views' % views)

        date = _format_date(info.get('upload_date'))
        if date:
            bits.append(date)

        max_tags = self.registryValue('maxHashtags', channel, network)
        tags = info.get('tags') or []
        if max_tags > 0 and tags:
            seen = set()
            hashtags = []
            for t in tags:
                h = _hashtagify(t)
                if h and len(h) > 1 and h not in seen:
                    seen.add(h)
                    hashtags.append(h)
                    if len(hashtags) >= max_tags:
                        break
            if hashtags:
                bits.append(' '.join(hashtags))

        line = ' • '.join(bits)
        # Touch the global value first so its __call__ refreshes from
        # _cache after `@config reload` and cascades to per-channel/network
        # children. registryValue(channel, network) goes straight to the
        # leaf, missing the refresh otherwise.
        try:
            conf.supybot.plugins.YouTube.prefix()
        except Exception:
            pass
        prefix = self.registryValue('prefix', channel, network)
        return (prefix + line) if prefix else line

    def _checkShrink(self, irc, url, channel, network):
        """Returns (should_shrink, was_cleaned). Always shrink if tracking
        was removed; otherwise honor ShrinkUrl.minimumLength.
        """
        was_cleaned = False
        cb = irc.getCallback('ShrinkUrl')
        if cb is not None:
            try:
                _cleaned, was_cleaned = cb._cleanUrl(url)
            except Exception:
                self.log.exception('YouTube: _cleanUrl failed')
        if was_cleaned:
            return (True, True)
        try:
            minlen = conf.supybot.plugins.ShrinkUrl.minimumLength \
                .get(network).get(channel)()
        except Exception:
            try:
                minlen = conf.supybot.plugins.ShrinkUrl.minimumLength()
            except Exception:
                minlen = 0
        return (len(url) >= int(minlen or 0), False)

    def _shrink(self, irc, url, channel, network):
        cb = irc.getCallback('ShrinkUrl')
        if cb is None:
            return None
        try:
            service = conf.supybot.plugins.ShrinkUrl.default \
                .get(network).get(channel)()
        except Exception:
            try:
                service = conf.supybot.plugins.ShrinkUrl.default()
            except Exception:
                service = 'tly'
        method = getattr(cb, '_get%sUrl' % service.capitalize(), None)
        if method is None:
            self.log.warning(
                'YouTube: ShrinkUrl has no method for service %r', service)
            return None
        try:
            cleaned, _was = cb._cleanUrl(url)
            return method(cleaned)
        except Exception:
            self.log.exception('YouTube: shrink failed for %s', url)
            return None

    def _handle(self, irc, channel, network, url):
        try:
            timeout = self.registryValue('timeout', channel, network)
            cookies = self.registryValue('cookiesFile', channel, network)
            info = self._fetchInfo(url, timeout, cookies or None)
            if info:
                line = self._formatInfo(info, channel, network)
                if line:
                    irc.queueMsg(ircmsgs.privmsg(channel, line))
            if self.registryValue('shrink', channel, network):
                should, was_cleaned = self._checkShrink(
                    irc, url, channel, network)
                if should:
                    short = self._shrink(irc, url, channel, network)
                    if short:
                        if self.registryValue('shrinkBold',
                                              channel, network):
                            short = ircutils.bold(short)
                        if was_cleaned:
                            short = '%s (untracked)' % short
                        irc.queueMsg(ircmsgs.privmsg(channel, short))
        except Exception:
            self.log.exception('YouTube: unhandled error in _handle')

    def youtubeSnarfer(self, irc, msg, match):
        channel = msg.channel
        network = irc.network
        if not channel:
            return
        if callbacks.addressed(irc, msg):
            return
        if not self.registryValue('snarfer', channel, network):
            return
        url = match.group(0)
        url = re.sub(r'[.,;:!?\)>\]]+$', '', url)
        threading.Thread(
            target=self._handle,
            args=(irc, channel, network, url),
            name='YouTube-Snarfer',
            daemon=True,
        ).start()
    youtubeSnarfer.__doc__ = YOUTUBE_RE.pattern


Class = YouTube

# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
