import html
import http.cookiejar
import ipaddress
import os
import re
import socket
import threading
import time
import urllib.parse

import supybot.callbacks as callbacks
import supybot.ircdb as ircdb
import supybot.ircmsgs as ircmsgs
import supybot.utils as utils
from supybot.commands import wrap

try:
    from curl_cffi import requests as cc
    _HAVE_CURLCFFI = True
except Exception:
    _HAVE_CURLCFFI = False
    cc = None


# Limnoria's utils.web.httpUrlRe mis-parses URLs containing '@' in the path
# (e.g. Google Maps coordinates like /maps/@lat,lon) because its optional
# userinfo group (\S+@) gets consumed by the path. Use a permissive matcher
# that just stops at whitespace or closing brackets.
_HTTP_URL_RE = re.compile(r'https?://[^\s\])>]+')

# Skip URLs whose path ends in an obvious media/binary extension — no
# point fetching or shortening these (no <title>, the resulting
# ":: <url>" / "<short>" lines are just noise).
_SKIP_EXT_RE = re.compile(
    r'\.(?:jpe?g|png|gif|webp|bmp|svg|ico|tiff?|heic|avif|'
    r'mp4|webm|mkv|mov|avi|m4v|wmv|flv|'
    r'mp3|ogg|flac|wav|m4a|opus|aac|'
    r'pdf|zip|tar|gz|tgz|xz|7z|rar|bz2|'
    r'iso|exe|dmg|deb|rpm|apk)$',
    re.IGNORECASE,
)

# Content-Types we never want to download a body for. HTML/XML filtering
# for the title path is separate, in fetch_title.
_BINARY_CT_RE = re.compile(r'^\s*(?:image|video|audio)/', re.IGNORECASE)

_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
_META_CHARSET_RE = re.compile(
    r'<meta[^>]+charset\s*=\s*["\']?([\w\-]+)', re.IGNORECASE)
_OG_TITLE_RE_A = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]*content=["\']([^"\']*)["\']',
    re.IGNORECASE)
_OG_TITLE_RE_B = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']og:title["\']',
    re.IGNORECASE)
_SHREDDIT_TITLE_RE = re.compile(
    r'<shreddit-post[^>]+post-title=["\']([^"\']+)["\']', re.IGNORECASE)
_TRAILING_PUNCT = ').,;:!?\'"'

_IMPERSONATE = "chrome131"
_MAX_HOPS = 5
_session_lock = threading.Lock()
_session = None
_cookies_loaded = None  # (path, mtime) of the file loaded into _session


def _maybe_load_cookies(path):
    """If path is a non-empty existing Netscape cookies file and is newer
    than what we last loaded, merge it into the shared session's jar.
    Caller must hold _session_lock."""
    global _cookies_loaded
    if not path:
        return
    try:
        st = os.stat(path)
    except OSError:
        return
    sig = (path, st.st_mtime)
    if _cookies_loaded == sig:
        return
    jar = http.cookiejar.MozillaCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        return
    for c in jar:
        try:
            _session.cookies.jar.set_cookie(c)
        except Exception:
            pass
    _cookies_loaded = sig


def _ip_is_safe(ip):
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in ipaddress.ip_network('100.64.0.0/10'):  # CGNAT
            return False
    return True


def _resolve_safe(host):
    """Resolve host; if every address is global, return one of them as a
    string. Otherwise return None (unsafe)."""
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    if not infos:
        return None
    chosen = None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            return None
        if not _ip_is_safe(ip):
            return None
        if chosen is None:
            chosen = info[4][0]
    return chosen


def _decode_title(body, content_type):
    charset = None
    ct_lower = (content_type or '').lower()
    if 'charset=' in ct_lower:
        charset = ct_lower.split('charset=', 1)[1].split(';', 1)[0].strip()
    if not charset:
        m = _META_CHARSET_RE.search(body[:4096].decode('latin-1', errors='replace'))
        if m:
            charset = m.group(1).strip()
    if not charset:
        charset = 'utf-8'
    try:
        text = body.decode(charset, errors='replace')
    except LookupError:
        text = body.decode('utf-8', errors='replace')

    def _clean(s):
        s = html.unescape(s)
        s = s.replace('\x00', '').replace('\r', ' ').replace('\n', ' ')
        return re.sub(r'\s+', ' ', s).strip() or None

    m = _TITLE_RE.search(text)
    title = _clean(m.group(1)) if m else None
    # Reddit's new web app emits a generic <title> and the actual post
    # title only in a custom element attribute / og:title meta.
    if not title or 'reddit - the heart' in title.lower():
        for rx in (_SHREDDIT_TITLE_RE, _OG_TITLE_RE_A, _OG_TITLE_RE_B):
            m2 = rx.search(text)
            if m2:
                t = _clean(m2.group(1))
                if t:
                    return t
    return title


def _do_one_get(url, referer, timeout, max_bytes, cookies_file=None):
    """Single GET via the shared curl_cffi Session, no auto-redirects.
    Returns (response_or_None, parsed_url_or_None).

    Note: SSRF-safety is enforced by `_resolve_safe(host)` before the call;
    curl will re-resolve on its own at connect time, so there's a tiny
    TOCTOU window but it's not a realistic risk for an IRC snarfer."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https'):
        return None, None
    host = parsed.hostname or ''
    if _resolve_safe(host) is None:
        return None, None
    headers = {'Referer': referer} if referer else {}
    global _session
    with _session_lock:
        if _session is None:
            _session = cc.Session(impersonate=_IMPERSONATE)
        _maybe_load_cookies(cookies_file)
        try:
            r = _session.get(
                url, timeout=timeout, allow_redirects=False,
                headers=headers, stream=True,
            )
            try:
                # Bail before downloading the body on image/video/audio
                # responses. Headers are available pre-body when streaming.
                ct_early = (r.headers.get('content-type')
                            or r.headers.get('Content-Type') or '')
                if _BINARY_CT_RE.match(ct_early):
                    return None, None
                buf = bytearray()
                for chunk in r.iter_content(chunk_size=16384):
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) >= max_bytes:
                        break
                r._cached_body = bytes(buf)
            finally:
                try:
                    r.close()
                except Exception:
                    pass
        except Exception:
            return None, None
    return r, parsed


def _walk_redirects(url, *, timeout, max_bytes, referer=None, cookies_file=None):
    """Walk up to _MAX_HOPS redirects, re-validating the host on each hop.
    Returns (final_response, final_parsed) or None on failure."""
    visited = set()
    cur = url
    cur_referer = referer
    for _ in range(_MAX_HOPS + 1):
        if cur in visited:
            return None
        visited.add(cur)
        r, parsed = _do_one_get(cur, cur_referer, timeout, max_bytes, cookies_file)
        if r is None:
            return None
        if r.status_code not in (301, 302, 303, 307, 308):
            return r, parsed
        loc = r.headers.get('Location') or r.headers.get('location')
        if not loc:
            return None
        cur = urllib.parse.urljoin(cur, loc)
        cur_referer = None
    return None


def _rewrite_for_fetch(url):
    """Transparently rewrite some hosts to a variant that returns a
    <title>-bearing page within our maxBytes budget."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    if host in ('www.reddit.com', 'reddit.com'):
        return urllib.parse.urlunsplit(parsed._replace(netloc='old.reddit.com'))
    return url


_TWITTER_HOSTS = {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com',
                  'mobile.twitter.com'}
_TWEET_PATH_RE = re.compile(r'^/[^/]+/status/(\d+)')


def _fetch_tweet_via_fxapi(url, *, timeout):
    """If url points at a tweet status, fetch its content via the
    api.fxtwitter.com JSON endpoint and format it as
    '@handle: "text"'. Returns None if not applicable or on error."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    if host not in _TWITTER_HOSTS:
        return None
    m = _TWEET_PATH_RE.match(parsed.path or '')
    if not m:
        return None
    tweet_id = m.group(1)
    api_url = f'https://api.fxtwitter.com/status/{tweet_id}'
    if _resolve_safe('api.fxtwitter.com') is None:
        return None
    global _session
    with _session_lock:
        if _session is None:
            _session = cc.Session(impersonate=_IMPERSONATE)
        try:
            r = _session.get(api_url, timeout=timeout, allow_redirects=False)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None
    tw = (data or {}).get('tweet') or {}
    text = (tw.get('text') or '').replace('\r', ' ').replace('\n', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    author = tw.get('author') or {}
    handle = (author.get('screen_name') or '').strip()
    if not text and not handle:
        return None
    if handle and text:
        return f'@{handle}: "{text}"'
    return f'@{handle}' if handle else text or None


def fetch_title(url, *, timeout=6.0, max_bytes=262144, user_agent=None,
                cookies_file=None):
    """Fetch the HTML <title> for url. user_agent is ignored when curl_cffi
    is available — impersonation dictates the headers."""
    if not _HAVE_CURLCFFI:
        return None
    tweet = _fetch_tweet_via_fxapi(url, timeout=timeout)
    if tweet is not None:
        return tweet
    url = _rewrite_for_fetch(url)
    parsed_init = urllib.parse.urlsplit(url)
    if parsed_init.scheme not in ('http', 'https'):
        return None

    walk = _walk_redirects(url, timeout=timeout, max_bytes=max_bytes,
                           cookies_file=cookies_file)
    if walk is None:
        return None
    r, parsed = walk

    if r.status_code == 403:
        # Akamai/CF-style anti-bot: warm up the session by hitting the host
        # root, which sets bot-manager cookies; then retry the original URL
        # with a Referer.
        warm_url = f"{parsed.scheme}://{parsed.hostname}/"
        _do_one_get(warm_url, None, timeout, max_bytes, cookies_file)
        walk = _walk_redirects(url, timeout=timeout, max_bytes=max_bytes,
                               referer=warm_url, cookies_file=cookies_file)
        if walk is None:
            return None
        r, parsed = walk
        if r.status_code != 200:
            return None
    elif r.status_code != 200:
        return None

    ct = (r.headers.get('content-type')
          or r.headers.get('Content-Type') or '')
    if 'html' not in ct.lower() and 'xml' not in ct.lower():
        return None
    body = getattr(r, '_cached_body', b'') or b''
    return _decode_title(body, ct)


def _truncate_bytes(s, limit):
    enc = s.encode('utf-8')
    if len(enc) <= limit:
        return s
    cut_at = max(0, limit - 3)
    cut = enc[:cut_at].decode('utf-8', errors='ignore').rstrip()
    return cut + '…'


def _strip_trailing_punct(url):
    while url and url[-1] in _TRAILING_PUNCT:
        url = url[:-1]
    return url


class _RecentURLs:
    def __init__(self, ttl=60):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._d = {}

    def seen(self, channel, url):
        now = time.monotonic()
        with self._lock:
            stale = [k for k, t in self._d.items() if now - t > self._ttl]
            for k in stale:
                del self._d[k]
            key = (channel, url)
            if key in self._d:
                return True
            self._d[key] = now
            return False


class Title(callbacks.Plugin):
    """Posts the HTML <title> of URLs mentioned in channel.

    Per-channel toggle:  !config channel #foo plugins.Title.enable True
    Skip pattern:        !config channel #foo plugins.Title.nonSnarfingRegexp m/youtube/i
    """
    threaded = True

    def __init__(self, irc):
        super().__init__(irc)
        self._recent = _RecentURLs(ttl=60)
        if not _HAVE_CURLCFFI:
            self.log.warning(
                "Title: curl_cffi not available; fetches will return None. "
                "Install with `pipx inject limnoria curl_cffi`.")

    def doPrivmsg(self, irc, msg):
        channel = msg.channel
        if not channel:
            return
        if msg.tagged('isCtcp') and not ircmsgs.isAction(msg):
            return
        if msg.nick == irc.nick:
            return
        try:
            if ircdb.channels.getChannel(channel).lobotomized:
                return
        except KeyError:
            pass
        if not self.registryValue('enable', channel, irc.network):
            return
        text = msg.args[1] if len(msg.args) > 1 else ''
        if not text:
            return
        m = _HTTP_URL_RE.search(text)
        if not m:
            return
        url = _strip_trailing_punct(m.group(0))
        if not url:
            return
        try:
            url_path = urllib.parse.urlsplit(url).path or ''
        except ValueError:
            return
        if _SKIP_EXT_RE.search(url_path):
            return
        skip_re = self.registryValue('nonSnarfingRegexp', channel, irc.network)
        if skip_re and skip_re.search(url):
            return
        if self._recent.seen(channel, url):
            return
        threading.Thread(
            target=self._do_fetch, args=(irc, channel, url),
            daemon=True, name='title-snarf'
        ).start()

    def _shorten_via_shrinkurl(self, irc, channel, network, url):
        """Ask the ShrinkUrl plugin to shorten <url> using its configured
        service. Returns the short URL or None (skipped, plugin missing,
        URL too short, service error)."""
        cb = irc.getCallback('ShrinkUrl')
        if cb is None:
            return None
        try:
            minlen = cb.registryValue('minimumLength', channel, network)
            if len(url) < minlen:
                return None
            try:
                rot = cb.registryValue(
                    'serviceRotation', channel, network, value=False)
                service = rot.getService().capitalize()
            except (ValueError, AttributeError):
                service = cb.registryValue(
                    'default', channel, network).capitalize()
            method = getattr(cb, '_get%sUrl' % service, None)
            if method is None:
                return None
            return method(url)
        except Exception:
            self.log.exception('ShrinkUrl integration failed for %s', url)
            return None

    def _do_fetch(self, irc, channel, url):
        network = irc.network
        try:
            title = fetch_title(
                url,
                timeout=self.registryValue('timeout'),
                max_bytes=self.registryValue('maxBytes'),
                cookies_file=self.registryValue('cookiesFile') or None)
        except Exception:
            self.log.exception('Error snarfing %s', url)
            title = None

        short = None
        if self.registryValue('useShrinkUrl', channel, network):
            short = self._shorten_via_shrinkurl(irc, channel, network, url)

        if short and title:
            line = f"{short} | {title}"
        elif title:
            fmt = self.registryValue('format', channel, network) or ':: %s'
            try:
                line = fmt % title
            except (TypeError, ValueError):
                line = ':: ' + title
        elif short:
            line = short
        else:
            return

        line = line.replace('\r', ' ').replace('\n', ' ').replace('\x00', '')
        line = _truncate_bytes(line, self.registryValue('maxLength'))
        irc.queueMsg(ircmsgs.privmsg(channel, line))

    def title(self, irc, msg, args, url):
        """<url>

        Manually fetch and reply with the <title> of <url>.
        """
        try:
            t = fetch_title(
                url,
                timeout=self.registryValue('timeout'),
                max_bytes=self.registryValue('maxBytes'),
                cookies_file=self.registryValue('cookiesFile') or None)
        except Exception:
            irc.error('fetch failed')
            return
        if not t:
            irc.reply('no title found.')
            return
        irc.reply(_truncate_bytes(t, self.registryValue('maxLength')))
    title = wrap(title, ['httpUrl'])


Class = Title
