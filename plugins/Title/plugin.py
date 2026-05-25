import collections
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
import supybot.ircutils as ircutils
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
# that just stops at whitespace or closing brackets. Balanced (...) is
# allowed inside the URL so Wikipedia disambiguators like
# /wiki/Foo_(footballer) are captured intact, while a trailing ')' that
# only wraps the URL in prose still stops the match.
_HTTP_URL_RE = re.compile(r'https?://(?:\([^\s()<>\]]*\)|[^\s()<>\]])+')

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

# Localhost Cloudflare WARP SOCKS5 proxy. Used as a retry route when a
# direct fetch returns a bot-challenge page (PerimeterX / Cloudflare /
# Akamai) — the WARP egress often gets the real content. See
# project_cloudflare_warp.
_WARP_PROXY = 'socks5h://127.0.0.1:40000'
_WARP_PROXIES = {'http': _WARP_PROXY, 'https': _WARP_PROXY}

# <title> values served by common anti-bot interstitials. If we extract one
# of these, the direct fetch lost to the gate and we should retry via WARP.
_BOT_CHALLENGE_TITLE_RE = re.compile(
    r'^\s*(?:'
    r'client challenge'
    r'|just a moment'
    r'|attention required'
    r'|checking your browser'
    r'|access denied'
    r'|verify you are a human'
    r'|please wait\.\.\.'
    r')',
    re.IGNORECASE)


def _is_challenge_title(title):
    return bool(title) and bool(_BOT_CHALLENGE_TITLE_RE.match(title))


_META_TAG_RE = re.compile(r'<meta\b[^>]*>', re.IGNORECASE)
_REFRESH_URL_RE = re.compile(
    r'url\s*=\s*(?:\'([^\']+)\'|"([^"]+)"|([^\s"\'>]+))', re.IGNORECASE)


def _meta_refresh_target(body, parsed):
    """If body is a <meta http-equiv=refresh> interstitial, return the
    absolute refresh URL (resolved against parsed), else None. Anti-bot
    gates (e.g. Akamai bot-manager's "bm-verify" page on justice.gov) 200
    with such a page and an empty <title>; following the refresh on the
    same (now cookie-warmed) session reaches the real content."""
    try:
        head = body[:8192].decode('latin-1', 'ignore')
    except Exception:
        return None
    for m in _META_TAG_RE.finditer(head):
        tag = m.group(0)
        if 'refresh' not in tag.lower():
            continue
        um = _REFRESH_URL_RE.search(tag)
        if not um:
            continue
        target = (um.group(1) or um.group(2) or um.group(3) or '').strip()
        if not target:
            continue
        return urllib.parse.urljoin(urllib.parse.urlunsplit(parsed), target)
    return None


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


def _do_one_get(url, referer, timeout, max_bytes, cookies_file=None,
                force_warp=False):
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
            kw = dict(timeout=timeout, allow_redirects=False,
                      headers=headers, stream=True)
            if force_warp:
                kw['proxies'] = _WARP_PROXIES
            r = _session.get(url, **kw)
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


def _walk_redirects(url, *, timeout, max_bytes, referer=None, cookies_file=None,
                    force_warp=False):
    """Walk up to _MAX_HOPS redirects, re-validating the host on each hop.
    Returns (final_response, final_parsed) or None on failure."""
    visited = set()
    cur = url
    cur_referer = referer
    for _ in range(_MAX_HOPS + 1):
        if cur in visited:
            return None
        visited.add(cur)
        r, parsed = _do_one_get(cur, cur_referer, timeout, max_bytes,
                                cookies_file, force_warp=force_warp)
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


_REDDIT_SUB_PATH_RE = re.compile(r'^/r/([A-Za-z0-9_]+)(?:/|$)')


def _reddit_subreddit(url):
    """If url is a reddit /r/<sub>/... link, return the subreddit name
    (as it appears in the URL); else None."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (parsed.hostname or '').lower()
    if not (host == 'reddit.com' or host.endswith('.reddit.com')):
        return None
    m = _REDDIT_SUB_PATH_RE.match(parsed.path or '')
    return m.group(1) if m else None


def _strip_reddit_suffix(title, url):
    """Old.reddit pages title posts as '<post title> : <subreddit>'.
    Strip the trailing ' : <subreddit>' when the URL is a reddit /r/<sub>
    link and the suffix matches that subreddit (case-insensitive)."""
    if not title:
        return title
    sub = _reddit_subreddit(url)
    if not sub:
        return title
    return re.sub(r'\s*:\s*' + re.escape(sub) + r'\s*$', '', title,
                  flags=re.IGNORECASE).strip() or title


# orange ▲ on white bg, then white 'reddit' on orange bg, then color reset.
# Matches the visual weight of YouTube.prefix. Used by the reddit special
# path — reddit interpolates /r/<sub> after the logo, so it isn't in the
# brand dispatch table below.
_REDDIT_LOGO = '\x0307,00 ▲ \x0300,07 reddit \x03 '


_TWITTER_HOSTS = {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com',
                  'mobile.twitter.com'}
_TWEET_PATH_RE = re.compile(r'^/[^/]+/status/(\d+)')


def _is_tweet_url(url):
    """True iff url is a tweet status link (x.com/twitter.com /<user>/status/<id>)."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or '').lower()
    if host not in _TWITTER_HOSTS:
        return False
    return bool(_TWEET_PATH_RE.match(parsed.path or ''))


# bold white X on black bg — matches X.com's branding. Tweets use a
# dedicated formatter (handle + quoted text), so the X logo isn't in the
# brand dispatch table.
_X_LOGO = '\x0300,01\x02 𝕏 \x02\x03 '


# ----------------------------------------------------------------------
# Brand prefixes: data-driven dispatch for sites that should get a
# colored logo and an optional title-suffix strip. Adding a new site is
# one entry in _BRANDS — define a logo string, a tail/head regex, and a
# host predicate built with _host_predicate().
# ----------------------------------------------------------------------

def _host_predicate(*, exact=(), suffix=(), labels=()):
    """Return a callable(url) -> bool. The url's hostname (lowercased,
    'www.' stripped) matches if it equals an entry in `exact`, or ends
    with any string in `suffix` (a leading '.' matches both 'foo.com'
    and 'sub.foo.com'), or contains any of `labels` as a DNS label
    (useful for multi-TLD brands like ebay.* or amazon.*)."""
    exact = frozenset(exact)
    suffix = tuple(suffix)
    labels = frozenset(labels)
    def f(url):
        try:
            h = (urllib.parse.urlsplit(url).hostname or '').lower()
        except ValueError:
            return False
        if not h:
            return False
        if h.startswith('www.'):
            h = h[4:]
        if h in exact:
            return True
        for s in suffix:
            bare = s[1:] if s.startswith('.') else s
            if h == bare or h.endswith(s):
                return True
        if labels and (set(h.split('.')) & labels):
            return True
        return False
    return f


Brand = collections.namedtuple('Brand', ['match', 'logo', 'tail_re', 'head_re'])


def _strip_brand_suffix(brand, title):
    """Strip leading head_re and trailing tail_re from title. Returns the
    original title if stripping yields an empty string."""
    if not title:
        return title
    t = title
    if brand.head_re is not None:
        t = brand.head_re.sub('', t)
    if brand.tail_re is not None:
        t = brand.tail_re.sub('', t)
    return t.strip() or title


# ----- Logo strings (mIRC: \x03FG,BG color, \x02 bold) -----
_EBAY_LOGO = (
    '\x02\x0304,01 e'    # red e
    '\x0312,01b'         # blue b
    '\x0308,01a'         # yellow a
    '\x0303,01y \x03\x02 '  # green y, on black bg
)
_FB_LOGO       = '\x0300,12\x02 f \x02\x03 '
_TIKTOK_LOGO   = '\x02\x0311,01 ♪\x0313,01♪ \x03\x02 '
_IG_LOGO       = '\x02\x0313,01 I\x0307,01G \x03\x02 '
_GH_LOGO       = '\x0300,01\x02 GH \x02\x03 '
_BSKY_LOGO     = '\x0300,12\x02 🦋 \x02\x03 '
_TWITCH_LOGO   = '\x0300,06\x02 Tw \x02\x03 '
_IMDB_LOGO     = '\x0301,08\x02 IMDb \x02\x03 '
_AMAZON_LOGO   = '\x0300,07\x02 a \x02\x03 '
_STEAM_LOGO    = '\x0300,02\x02 ⛁ \x02\x03 '
_THREADS_LOGO  = '\x0300,01\x02 @ \x02\x03 '
_LINKEDIN_LOGO = '\x0300,12\x02 in \x02\x03 '
_GITLAB_LOGO   = '\x0300,07\x02 GL \x02\x03 '
_SPOTIFY_LOGO  = '\x0301,03\x02 ♫ \x02\x03 '
_BANDCAMP_LOGO = '\x0300,10\x02 BC \x02\x03 '
_SOUNDCLOUD_LOGO = '\x0300,07\x02 ☁ \x02\x03 '
_IMGUR_LOGO    = '\x0309,01\x02 i. \x02\x03 '
_BBC_LOGO      = '\x0300,04\x02 BBC \x02\x03 '
_CNN_LOGO      = '\x0300,04\x02 CNN \x02\x03 '
_NYT_LOGO      = '\x0300,01\x02 NYT \x02\x03 '
_GUARDIAN_LOGO = '\x0300,02\x02 G \x02\x03 '
_SPIEGEL_LOGO  = '\x0300,04\x02 S \x02\x03 '
_TAGESSCHAU_LOGO = '\x0300,02\x02 TS \x02\x03 '
_HEISE_LOGO    = '\x0300,04\x02 h: \x02\x03 '
_HN_LOGO       = '\x0301,07\x02 Y \x02\x03 '
_WIKIPEDIA_LOGO = '\x0301,00\x02 W \x02\x03 '
_ZDNET_LOGO    = '\x0301,09\x02 ZD \x02\x03 '


# ----- Title strip patterns (case-insensitive) -----
_EBAY_TAIL     = re.compile(r'\s*[|\-:·]\s*eBay(?:\.[A-Za-z.]+)?\s*$', re.I)
_FB_TAIL       = re.compile(r'\s*[|\-:·]\s*Facebook\s*$', re.I)
_TIKTOK_TAIL   = re.compile(r'\s*[|\-:·]\s*TikTok\s*$', re.I)
_IG_TAIL       = re.compile(
    r'\s*[|\-:·•]\s*Instagram(?:\s+photos\s+and\s+videos)?\s*$', re.I)
_GH_TAIL       = re.compile(r'\s*[|\-:·]\s*GitHub\s*$', re.I)
_BSKY_TAIL     = re.compile(r'\s*(?:[|\-:·—]\s*Bluesky|on\s+Bluesky)\s*$', re.I)
_TWITCH_TAIL   = re.compile(r'\s*[|\-:·]\s*Twitch\s*$', re.I)
_IMDB_TAIL     = re.compile(r'\s*[|\-:·—]\s*IMDb\s*$', re.I)
_AMAZON_TAIL   = re.compile(
    r'\s*[|\-:·—–]\s*Amazon(?:\.[A-Za-z.]+)?\s*$', re.I)
_AMAZON_HEAD   = re.compile(r'^Amazon(?:\.[A-Za-z.]+)?\s*:\s*', re.I)
_STEAM_TAIL    = re.compile(r'\s*(?:on\s+Steam|[|\-:·]\s*Steam)\s*$', re.I)
_THREADS_TAIL  = re.compile(
    r'\s*(?:on\s+Threads|[|\-:·•]\s*Threads)\s*$', re.I)
_LINKEDIN_TAIL = re.compile(r'\s*[|\-:·]\s*LinkedIn\s*$', re.I)
_GITLAB_TAIL   = re.compile(r'\s*[|\-:·]\s*GitLab\s*$', re.I)
_SPOTIFY_TAIL  = re.compile(r'\s*[|\-:·]\s*Spotify\s*$', re.I)
_BANDCAMP_TAIL = re.compile(r'\s*[|\-:·]\s*Bandcamp\s*$', re.I)
_SOUNDCLOUD_TAIL = re.compile(
    r'\s*[|\-:·]\s*(?:Free\s+Listening\s+on\s+)?SoundCloud\s*$', re.I)
_IMGUR_TAIL    = re.compile(
    r'\s*(?:[|\-:·—]\s*(?:Album\s+on\s+)?Imgur|on\s+Imgur)\s*$', re.I)
_IMGUR_HEAD    = re.compile(r'^Imgur\s*:\s*', re.I)
_BBC_TAIL      = re.compile(
    r'\s*[|\-:·—]\s*BBC(?:\s+(?:News|Sport|Weather|iPlayer|Sounds))?\s*$',
    re.I)
_CNN_TAIL      = re.compile(
    r'\s*[|\-:·—]\s*CNN(?:\s+(?:Business|Politics|Health|Style|Travel))?\s*$',
    re.I)
_NYT_TAIL      = re.compile(
    r'\s*[|\-:·—–]\s*The\s+New\s+York\s+Times\s*$', re.I)
_GUARDIAN_TAIL = re.compile(
    r'\s*[|\-:·—–]\s*The\s+Guardian\s*$', re.I)
_SPIEGEL_TAIL  = re.compile(
    r'\s*[|\-:·—–]\s*DER\s+SPIEGEL\s*$', re.I)
_TAGESSCHAU_TAIL = re.compile(
    r'\s*[|\-:·—]\s*tagesschau(?:\.de)?\s*$', re.I)
_HEISE_TAIL    = re.compile(
    r'\s*[|\-:·—]\s*heise(?:\s+online|\+)?\s*$', re.I)
_HN_TAIL       = re.compile(
    r'\s*[|\-:·—]\s*Hacker\s+News\s*$', re.I)
_WIKIPEDIA_TAIL = re.compile(
    r'\s*[|\-:·—–]\s*Wikipedia(?:,\s+the\s+free\s+encyclopedia)?\s*$', re.I)
_ZDNET_TAIL    = re.compile(r'\s*[|\-:·—–]\s*ZDNET\s*$', re.I)


_BRANDS = (
    Brand(_host_predicate(labels={'ebay'}),
          _EBAY_LOGO, _EBAY_TAIL, None),
    Brand(_host_predicate(exact={'facebook.com', 'fb.com', 'fb.me'},
                          suffix={'.facebook.com'}),
          _FB_LOGO, _FB_TAIL, None),
    Brand(_host_predicate(exact={'tiktok.com'}, suffix={'.tiktok.com'}),
          _TIKTOK_LOGO, _TIKTOK_TAIL, None),
    Brand(_host_predicate(exact={'instagram.com', 'instagr.am'},
                          suffix={'.instagram.com'}),
          _IG_LOGO, _IG_TAIL, None),
    Brand(_host_predicate(exact={'github.com'}, suffix={'.github.com'}),
          _GH_LOGO, _GH_TAIL, None),
    Brand(_host_predicate(exact={'bsky.app'}, suffix={'.bsky.app'}),
          _BSKY_LOGO, _BSKY_TAIL, None),
    Brand(_host_predicate(exact={'twitch.tv'}, suffix={'.twitch.tv'}),
          _TWITCH_LOGO, _TWITCH_TAIL, None),
    Brand(_host_predicate(exact={'imdb.com'}, suffix={'.imdb.com'}),
          _IMDB_LOGO, _IMDB_TAIL, None),
    Brand(_host_predicate(labels={'amazon'},
                          exact={'amzn.to', 'amzn.eu', 'amzn.com'}),
          _AMAZON_LOGO, _AMAZON_TAIL, _AMAZON_HEAD),
    Brand(_host_predicate(exact={'steamcommunity.com', 'steamdb.info'},
                          suffix={'.steampowered.com',
                                  '.steamcommunity.com'}),
          _STEAM_LOGO, _STEAM_TAIL, None),
    Brand(_host_predicate(exact={'threads.net', 'threads.com'},
                          suffix={'.threads.net', '.threads.com'}),
          _THREADS_LOGO, _THREADS_TAIL, None),
    Brand(_host_predicate(exact={'linkedin.com', 'lnkd.in'},
                          suffix={'.linkedin.com'}),
          _LINKEDIN_LOGO, _LINKEDIN_TAIL, None),
    Brand(_host_predicate(exact={'gitlab.com'}, suffix={'.gitlab.com'}),
          _GITLAB_LOGO, _GITLAB_TAIL, None),
    Brand(_host_predicate(exact={'spotify.com', 'spotify.link'},
                          suffix={'.spotify.com', '.spotify.link'}),
          _SPOTIFY_LOGO, _SPOTIFY_TAIL, None),
    Brand(_host_predicate(exact={'bandcamp.com'},
                          suffix={'.bandcamp.com'}),
          _BANDCAMP_LOGO, _BANDCAMP_TAIL, None),
    Brand(_host_predicate(exact={'soundcloud.com', 'snd.sc'},
                          suffix={'.soundcloud.com'}),
          _SOUNDCLOUD_LOGO, _SOUNDCLOUD_TAIL, None),
    Brand(_host_predicate(exact={'imgur.com'}, suffix={'.imgur.com'}),
          _IMGUR_LOGO, _IMGUR_TAIL, _IMGUR_HEAD),
    Brand(_host_predicate(exact={'bbc.com', 'bbc.co.uk'},
                          suffix={'.bbc.com', '.bbc.co.uk'}),
          _BBC_LOGO, _BBC_TAIL, None),
    Brand(_host_predicate(exact={'cnn.com', 'cnn.it'},
                          suffix={'.cnn.com'}),
          _CNN_LOGO, _CNN_TAIL, None),
    Brand(_host_predicate(exact={'nytimes.com', 'nyti.ms'},
                          suffix={'.nytimes.com'}),
          _NYT_LOGO, _NYT_TAIL, None),
    Brand(_host_predicate(exact={'theguardian.com', 'guardian.co.uk'},
                          suffix={'.theguardian.com', '.guardian.co.uk'}),
          _GUARDIAN_LOGO, _GUARDIAN_TAIL, None),
    Brand(_host_predicate(exact={'spiegel.de'},
                          suffix={'.spiegel.de'}),
          _SPIEGEL_LOGO, _SPIEGEL_TAIL, None),
    Brand(_host_predicate(exact={'tagesschau.de'},
                          suffix={'.tagesschau.de'}),
          _TAGESSCHAU_LOGO, _TAGESSCHAU_TAIL, None),
    Brand(_host_predicate(exact={'heise.de'},
                          suffix={'.heise.de'}),
          _HEISE_LOGO, _HEISE_TAIL, None),
    Brand(_host_predicate(exact={'news.ycombinator.com'}),
          _HN_LOGO, _HN_TAIL, None),
    Brand(_host_predicate(suffix={'.wikipedia.org'}),
          _WIKIPEDIA_LOGO, _WIKIPEDIA_TAIL, None),
    Brand(_host_predicate(labels={'zdnet'}),
          _ZDNET_LOGO, _ZDNET_TAIL, None),
)


def _match_brand(url):
    """Return the first Brand whose matcher accepts url, or None."""
    for b in _BRANDS:
        if b.match(url):
            return b
    return None


def _fetch_tweet_data(url, *, timeout):
    """If url points at a tweet status, fetch its content via the
    api.fxtwitter.com JSON endpoint. Returns dict {'handle': str, 'text': str}
    or None if not applicable / on error."""
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
    return {'handle': handle, 'text': text}


def _fetch_tweet_via_fxapi(url, *, timeout):
    """Legacy string form used by the manual .title command."""
    d = _fetch_tweet_data(url, timeout=timeout)
    if d is None:
        return None
    handle, text = d['handle'], d['text']
    if handle and text:
        return f'@{handle}: "{text}"'
    return f'@{handle}' if handle else (text or None)


def _attempt_fetch_title(url, *, timeout, max_bytes, cookies_file, force_warp):
    """One full fetch attempt (walk + warmup-on-403 + decode). Returns the
    cleaned title string, or None if this route couldn't produce one."""
    walk = _walk_redirects(url, timeout=timeout, max_bytes=max_bytes,
                           cookies_file=cookies_file, force_warp=force_warp)
    if walk is None:
        return None
    r, parsed = walk

    if r.status_code == 403:
        # Akamai/CF-style anti-bot: warm up the session by hitting the host
        # root, which sets bot-manager cookies; then retry the original URL
        # with a Referer.
        warm_url = f"{parsed.scheme}://{parsed.hostname}/"
        _do_one_get(warm_url, None, timeout, max_bytes, cookies_file,
                    force_warp=force_warp)
        walk = _walk_redirects(url, timeout=timeout, max_bytes=max_bytes,
                               referer=warm_url, cookies_file=cookies_file,
                               force_warp=force_warp)
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
    title = _decode_title(body, ct)

    # Anti-bot meta-refresh interstitial (e.g. Akamai bot-manager's
    # "bm-verify" gate used by justice.gov): the first 200 carries no real
    # <title> and a <meta refresh> to a verification URL. Follow it -- the
    # bot-manager cookies this session just received carry over -- to reach
    # the real page. Bounded to avoid interstitial loops.
    hops = 0
    while (not title or _is_challenge_title(title)) and hops < 2:
        target = _meta_refresh_target(body, parsed)
        if not target:
            break
        hops += 1
        walk = _walk_redirects(target, timeout=timeout, max_bytes=max_bytes,
                               referer=url, cookies_file=cookies_file,
                               force_warp=force_warp)
        if walk is None or walk[0].status_code != 200:
            break
        r, parsed = walk
        ct = (r.headers.get('content-type')
              or r.headers.get('Content-Type') or '')
        if 'html' not in ct.lower() and 'xml' not in ct.lower():
            break
        body = getattr(r, '_cached_body', b'') or b''
        title = _decode_title(body, ct)

    return _strip_reddit_suffix(title, url)


def fetch_title(url, *, timeout=6.0, max_bytes=262144, user_agent=None,
                cookies_file=None):
    """Fetch the HTML <title> for url. user_agent is ignored when curl_cffi
    is available — impersonation dictates the headers.

    Two-pass strategy: try a direct fetch first; if it fails outright or
    comes back with a bot-challenge title, retry once via the local WARP
    SOCKS5 proxy. Most sites are fine direct; the WARP retry rescues hosts
    whose bot-protection blacklists our VPS egress IP."""
    if not _HAVE_CURLCFFI:
        return None
    tweet = _fetch_tweet_via_fxapi(url, timeout=timeout)
    if tweet is not None:
        return tweet
    url = _rewrite_for_fetch(url)
    parsed_init = urllib.parse.urlsplit(url)
    if parsed_init.scheme not in ('http', 'https'):
        return None

    direct = _attempt_fetch_title(url, timeout=timeout, max_bytes=max_bytes,
                                  cookies_file=cookies_file, force_warp=False)
    if direct is not None and not _is_challenge_title(direct):
        return direct

    warp = _attempt_fetch_title(url, timeout=timeout, max_bytes=max_bytes,
                                cookies_file=cookies_file, force_warp=True)
    if warp is not None and not _is_challenge_title(warp):
        return warp
    # Neither route produced real content. Suppress the bot-challenge
    # string entirely rather than posting it.
    return None


def _truncate_bytes(s, limit):
    enc = s.encode('utf-8')
    if len(enc) <= limit:
        return s
    cut_at = max(0, limit - 3)
    cut = enc[:cut_at].decode('utf-8', errors='ignore').rstrip()
    return cut + '…'


def _truncate_text_word_aware(text, byte_budget):
    """Truncate `text` to fit within byte_budget UTF-8 bytes, preferring
    to break on a whitespace boundary. Appends '…' when truncation occurs."""
    enc = text.encode('utf-8')
    if len(enc) <= byte_budget:
        return text
    cut_at = max(0, byte_budget - 3)  # 3 bytes for '…'
    cut = enc[:cut_at].decode('utf-8', errors='ignore').rstrip()
    # Walk back to the last whitespace, but don't sacrifice too much text:
    # only honour the boundary if it lies within the final ~30 chars.
    i = cut.rfind(' ')
    if i >= 0 and (len(cut) - i) <= 30:
        cut = cut[:i].rstrip(' ,;:.!?-—–"\'')
    return cut + '…'


def _strip_trailing_punct(url):
    while url:
        c = url[-1]
        if c == ')':
            # Keep a trailing ')' if it balances a '(' inside the URL —
            # Wikipedia disambiguators like /Foo_(footballer) would
            # otherwise be truncated to /Foo_(footballer with no
            # closing paren. Strip it only if there is no matching '('.
            if url.count('(') > url.count(')') - 1:
                break
            url = url[:-1]
            continue
        if c in _TRAILING_PUNCT:
            url = url[:-1]
            continue
        break
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


class _RateLimiter:
    """Throttle title posts. Per-nick cooldown + per-channel window cap."""
    def __init__(self, nick_cooldown=20.0, chan_window=30.0, chan_max=3):
        self._nick_cooldown = nick_cooldown
        self._chan_window = chan_window
        self._chan_max = chan_max
        self._lock = threading.Lock()
        self._nick_last = {}
        self._chan_hits = {}

    def allow(self, channel, nick):
        now = time.monotonic()
        with self._lock:
            last = self._nick_last.get((channel, nick), 0.0)
            if now - last < self._nick_cooldown:
                return False
            hits = [t for t in self._chan_hits.get(channel, ())
                    if now - t < self._chan_window]
            if len(hits) >= self._chan_max:
                self._chan_hits[channel] = hits
                return False
            hits.append(now)
            self._chan_hits[channel] = hits
            self._nick_last[(channel, nick)] = now
            return True


class Title(callbacks.Plugin):
    """Posts the HTML <title> of URLs mentioned in channel.

    Per-channel toggle:  !config channel #foo plugins.Title.enable True
    Skip pattern:        !config channel #foo plugins.Title.nonSnarfingRegexp m/youtube/i
    """
    threaded = True

    def __init__(self, irc):
        super().__init__(irc)
        self._recent = _RecentURLs(ttl=10)
        self._rate = _RateLimiter(nick_cooldown=20.0, chan_window=30.0, chan_max=3)
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
        if not self._rate.allow(channel, msg.nick):
            self.log.info(
                "Title: rate-limited %s in %s (url=%s)", msg.nick, channel, url)
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

    def _format_tweet_line(self, tweet, short, max_len):
        """Assemble the snarfer line for a tweet, word-truncating the body
        to fit the byte budget and keeping the closing quote intact."""
        handle = (tweet.get('handle') or '').strip()
        text = (tweet.get('text') or '').strip()

        prefix = _X_LOGO
        if handle:
            prefix = f"{prefix}@{handle}: "

        suffix = ''
        if short:
            suffix = f' | {ircutils.mircColor(short, "12")}'

        if not text:
            return f"{prefix.rstrip()}{suffix}"

        overhead = len((prefix + '""' + suffix).encode('utf-8'))
        budget = max(1, max_len - overhead)
        body = _truncate_text_word_aware(text, budget)
        return f'{prefix}"{body}"{suffix}'

    def _compose_line(self, prefix, title, short, max_len):
        """Assemble '<prefix><title> | <short-url>' within max_len bytes.
        The shortened URL is appended last and never truncated; the title
        is word-truncated to whatever byte budget remains."""
        suffix = ''
        if short:
            suffix = f' | {ircutils.mircColor(short, "12")}'
        overhead = len((prefix + suffix).encode('utf-8'))
        budget = max(1, max_len - overhead)
        body = _truncate_text_word_aware(title, budget)
        return f'{prefix}{body}{suffix}'

    def _do_fetch(self, irc, channel, url):
        network = irc.network
        max_len = self.registryValue('maxLength')
        is_tweet = _is_tweet_url(url)

        tweet = None
        title = None
        if is_tweet:
            try:
                tweet = _fetch_tweet_data(
                    url, timeout=self.registryValue('timeout'))
            except Exception:
                self.log.exception('Error snarfing tweet %s', url)
                tweet = None
        else:
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

        sub = _reddit_subreddit(url)
        brand = _match_brand(url)

        if is_tweet and tweet is not None:
            line = self._format_tweet_line(tweet, short, max_len)
        elif brand is not None and title:
            title = _strip_brand_suffix(brand, title)
            line = self._compose_line(brand.logo, title, short, max_len)
        elif title:
            # Bare title (incl. reddit /r/sub prefix). Short URL is appended
            # last by _compose_line so it always trails the headline and is
            # never truncated away.
            prefix = ''
            if sub:
                prefix = f"{_REDDIT_LOGO}{ircutils.bold(f'/r/{sub}')} · "
            line = self._compose_line(prefix, title, short, max_len)
        elif short:
            line = short
            if sub:
                line = f"{_REDDIT_LOGO}{ircutils.bold(f'/r/{sub}')} · {line}"
            line = _truncate_bytes(line, max_len)
        else:
            return

        line = line.replace('\r', ' ').replace('\n', ' ').replace('\x00', '')
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
