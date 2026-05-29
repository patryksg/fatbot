###
# YouTube plugin: snarfer that posts video metadata via yt-dlp and a
# shrunk URL via the ShrinkUrl plugin.
#
# Also provides !ytdl <url> to download a YouTube video and host it on
# Zipline (img.example.net). Requires ZIPLINE_TOKEN / ZIPLINE_UPLOAD_URL /
# ZIPLINE_PUBLIC_BASE env vars (same as Create plugin).
#
# Designed to coexist with ShrinkUrl by handling both messages itself.
# Configure ShrinkUrl.nonSnarfingRegexp to skip YouTube URLs so the
# shrunk-URL line only fires once and in the right order.
###

import os
import re
import json
import uuid
import glob
import shutil
import tempfile
import subprocess
import threading
import urllib.request
import urllib.error

import supybot.conf as conf
import supybot.ircdb as ircdb
import supybot.utils as utils
import supybot.ircmsgs as ircmsgs
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
from supybot.commands import wrap
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

# mIRC color 12 = royal blue (readable on dark and light themes)
_BLUE = '\x0312'
_RESET = '\x03'


def _blue_link(url):
    return '%s%s%s' % (_BLUE, url, _RESET)


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


def _human_bytes(n):
    for unit, divisor in (('GB', 1024 ** 3), ('MB', 1024 ** 2), ('KB', 1024)):
        if n >= divisor:
            v = n / divisor
            s = ('%.1f' % v).rstrip('0').rstrip('.')
            return s + unit
    return '%dB' % n


class YouTube(callbacks.PluginRegexp):
    """Snarfer for YouTube video URLs, and !ytdl to download+host videos."""

    regexps = ['youtubeSnarfer']
    flags = re.IGNORECASE

    def __init__(self, irc):
        self.__parent = super(YouTube, self)
        self.__parent.__init__(irc)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # !ytdl helpers                                                        #
    # ------------------------------------------------------------------ #

    def _check_ytdl_cap(self, irc, msg):
        if not msg.channel:
            irc.error("channel-only")
            return False
        chan_cap = ircdb.makeChannelCapability(msg.channel, 'ytdl')
        if not ircdb.checkCapability(msg.prefix, chan_cap):
            irc.errorNoCapability(chan_cap)
            return False
        return True

    @staticmethod
    def _ext_for_mime(mime):
        table = {
            'video/mp4': 'mp4', 'video/webm': 'webm',
            'video/quicktime': 'mov', 'video/x-matroska': 'mkv',
        }
        return table.get((mime or '').lower().split(';')[0].strip(), 'mp4')

    def _zipline_upload_file(self, path, mime, timeout):
        """Upload a local file to Zipline. Returns the public URL."""
        token = os.environ.get('ZIPLINE_TOKEN')
        endpoint = os.environ.get('ZIPLINE_UPLOAD_URL')
        if not token or not endpoint:
            raise RuntimeError('ZIPLINE_TOKEN/ZIPLINE_UPLOAD_URL not set')
        ext = self._ext_for_mime(mime)
        with open(path, 'rb') as fh:
            raw = fh.read()
        boundary = '----fatbot' + uuid.uuid4().hex
        fname = uuid.uuid4().hex + '.' + ext
        head = (
            '--' + boundary + '\r\n'
            'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
            'Content-Type: %s\r\n\r\n' % (fname, mime or 'video/mp4')
        ).encode('utf-8')
        tail = ('\r\n--' + boundary + '--\r\n').encode('utf-8')
        data = head + raw + tail
        host = os.environ.get('ZIPLINE_HOST')
        headers = {
            'authorization': token,
            'content-type': 'multipart/form-data; boundary=' + boundary,
        }
        if host:
            headers['Host'] = host
        req = urllib.request.Request(
            endpoint, data=data, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                j = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8', 'replace')[:200]
            except Exception:
                pass
            raise RuntimeError('zipline http %d: %s' % (e.code, detail))
        except Exception as e:
            raise RuntimeError('zipline upload failed: %s' % e)
        files = j.get('files') or []
        if not files or 'url' not in files[0]:
            raise RuntimeError(
                'zipline: no url in response: %s' % json.dumps(j)[:200])
        url = files[0]['url']
        base = os.environ.get('ZIPLINE_PUBLIC_BASE')
        if base:
            from urllib.parse import urlsplit
            url = base.rstrip('/') + urlsplit(url).path
        return url

    def _do_ytdl(self, irc, channel, network, url):
        """Background worker: download URL, upload to Zipline, post link."""
        tmpdir = tempfile.mkdtemp(prefix='fatbot-ytdl-')
        try:
            cookies = self.registryValue('cookiesFile', channel, network)
            dl_timeout = self.registryValue('ytdlTimeout', channel, network)
            max_bytes = self.registryValue('ytdlMaxBytes', channel, network)

            # Build yt-dlp command — prefer mp4 so the upload plays inline
            cmd = [
                YT_DLP,
                '--no-playlist', '--no-warnings', '--socket-timeout', '15',
                '-f',
                ('bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]'
                 '/best[ext=mp4][height<=1080]'
                 '/bestvideo[height<=1080]+bestaudio'
                 '/best[height<=1080]/best'),
                '--merge-output-format', 'mp4',
            ]
            if max_bytes > 0:
                cmd += ['--max-filesize', '%d' % max_bytes]
            if cookies:
                cmd += ['--cookies', cookies]
            cmd += ['-o', os.path.join(tmpdir, '%(id)s.%(ext)s'), '--', url]

            self.log.info('YouTube.ytdl: running %s', ' '.join(cmd))
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=dl_timeout,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                irc.queueMsg(ircmsgs.privmsg(channel,
                    'ytdl: download timed out after %ds' % dl_timeout))
                return
            except Exception as e:
                irc.queueMsg(ircmsgs.privmsg(channel,
                    'ytdl: yt-dlp error: %s' % e))
                return

            if proc.returncode != 0:
                err = (proc.stderr or '').strip().splitlines()
                # grab the last meaningful line for the IRC error
                msg_line = next(
                    (l for l in reversed(err) if l.strip()), '') or 'download failed'
                irc.queueMsg(ircmsgs.privmsg(channel,
                    'ytdl: %s' % msg_line[:200]))
                return

            # Find downloaded file
            files = [f for f in glob.glob(os.path.join(tmpdir, '*'))
                     if os.path.isfile(f)]
            if not files:
                irc.queueMsg(ircmsgs.privmsg(channel,
                    'ytdl: no file produced by yt-dlp'))
                return
            dl_path = files[0]
            size = os.path.getsize(dl_path)

            if max_bytes > 0 and size > max_bytes:
                irc.queueMsg(ircmsgs.privmsg(channel,
                    'ytdl: file too large (%s > %s limit)' % (
                        _human_bytes(size), _human_bytes(max_bytes))))
                return

            # Determine MIME type from extension
            ext = os.path.splitext(dl_path)[1].lower().lstrip('.')
            mime_map = {
                'mp4': 'video/mp4', 'webm': 'video/webm',
                'mkv': 'video/x-matroska', 'mov': 'video/quicktime',
            }
            mime = mime_map.get(ext, 'video/mp4')

            self.log.info('YouTube.ytdl: uploading %s (%s, %s)',
                          dl_path, mime, _human_bytes(size))
            try:
                zip_url = self._zipline_upload_file(dl_path, mime, 120)
            except Exception as e:
                self.log.exception('YouTube.ytdl: upload failed')
                irc.queueMsg(ircmsgs.privmsg(channel,
                    'ytdl: upload failed: %s' % e))
                return

            # Shorten Zipline URL via ShrinkUrl chain
            short = None
            cb = irc.getCallback('ShrinkUrl')
            if cb is not None:
                try:
                    short = cb._getTlyUrl(zip_url)
                except Exception:
                    self.log.exception('YouTube.ytdl: shrink failed')

            link = _blue_link(short or zip_url)

            # Fetch metadata for title/duration display (best-effort)
            try:
                info = self._fetchInfo(
                    url, self.registryValue('timeout', channel, network),
                    cookies or None)
            except Exception:
                info = None
            bits = []
            if info:
                title = (info.get('title') or '').strip()
                if title:
                    bits.append(ircutils.bold(title))
                dur = _format_duration(info)
                if dur:
                    bits.append(dur)
                sz_str = _human_bytes(size)
                bits.append(sz_str)
            else:
                bits.append(_human_bytes(size))
            reply = ' • '.join(bits) + ' → ' + link
            irc.queueMsg(ircmsgs.privmsg(channel, reply))

        except Exception:
            self.log.exception('YouTube.ytdl: unhandled error')
            irc.queueMsg(ircmsgs.privmsg(channel,
                'ytdl: unexpected error (see logs)'))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Snarfer                                                              #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    def ytdl(self, irc, msg, args, url):
        """<youtube-url>

        Download a YouTube video and host it on img.example.net.
        Requires the 'ytdl' channel capability.
        """
        channel = msg.channel
        network = irc.network
        if not self._check_ytdl_cap(irc, msg):
            return
        url = url.strip()
        if not YOUTUBE_RE.search(url):
            irc.error("that doesn't look like a YouTube video URL")
            return
        irc.reply('Downloading...', prefixNick=False)
        threading.Thread(
            target=self._do_ytdl,
            args=(irc, channel, network, url),
            name='YouTube-DL',
            daemon=True,
        ).start()
    ytdl = wrap(ytdl, ['public', 'text'])


Class = YouTube

# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
