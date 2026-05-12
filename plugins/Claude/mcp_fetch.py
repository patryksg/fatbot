#!/usr/bin/env python3
"""MCP server exposing fetch_page(url) for arbitrary http(s) URLs.

Default egress is the local WARP SOCKS5 proxy (127.0.0.1:40000) so the
bot sees what a residential client would see (gets past most anti-bot
challenges on Hetzner-IP-blocked sites like merkur.de, reddit, etc.).
Falls back to direct egress only on connection-level WARP failures.

Returns plain text extracted from HTML (script/style stripped, tags
removed, whitespace collapsed), capped at ~8000 chars. SSRF-guarded
exactly like mcp_imageview: rejects non-global / CGNAT addresses,
revalidates host on each redirect hop.
"""

import hashlib
import html as html_mod
import ipaddress
import os
import re
import socket
import tempfile
import time
import urllib.parse
from html.parser import HTMLParser

from curl_cffi import requests as cc


_IMPERSONATE = "chrome131"
_PROXY = "socks5h://127.0.0.1:40000"
_MAX_HOPS = 6
_MAX_BYTES = 4 * 1024 * 1024  # 4 MB raw page cap before text extraction
_TIMEOUT = 15.0
_MAX_CHARS = 8000
_CACHE_TTL = 3600

_TEXTY_CTS = ("text/html", "application/xhtml", "text/plain", "application/json",
              "text/xml", "application/xml")

_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")
_DROP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "head"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "header", "footer", "nav", "aside", "blockquote",
               "pre", "hr", "ul", "ol", "dl", "dt", "dd", "table", "caption"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._skip_depth = 0
        self._skip_tag = None

    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in _DROP_TAGS:
            self._skip_tag = tag
            self._skip_depth = 1
            return
        if tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.out.append(data)


def _ip_is_safe(ip):
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return False
    return True


def _resolve_safe(host):
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            return False
        if not _ip_is_safe(ip):
            return False
    return True


_sessions = {}


def _session(use_proxy):
    key = "proxy" if use_proxy else "direct"
    if key not in _sessions:
        kw = {"impersonate": _IMPERSONATE}
        if use_proxy:
            kw["proxies"] = {"http": _PROXY, "https": _PROXY}
        _sessions[key] = cc.Session(**kw)
    return _sessions[key]


def _cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "claude-fetch")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _sweep(d):
    now = time.time()
    try:
        for n in os.listdir(d):
            p = os.path.join(d, n)
            try:
                if now - os.path.getmtime(p) > _CACHE_TTL:
                    os.unlink(p)
            except OSError:
                pass
    except OSError:
        pass


def _extract_text(html, ct):
    if "html" in ct or "xml" in ct:
        p = _TextExtractor()
        try:
            p.feed(html)
            p.close()
        except Exception:
            pass
        text = "".join(p.out)
    else:
        text = html
    text = html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def _fetch_once(url, use_proxy):
    visited = set()
    cur = url
    sess = _session(use_proxy)
    for _ in range(_MAX_HOPS + 1):
        if cur in visited:
            raise ValueError("redirect loop")
        visited.add(cur)
        parsed = urllib.parse.urlsplit(cur)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("only http(s) URLs accepted")
        if not _resolve_safe(parsed.hostname or ""):
            raise ValueError("host resolves to a non-public address")
        r = sess.get(
            cur,
            timeout=_TIMEOUT,
            allow_redirects=False,
            stream=True,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        try:
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location") or r.headers.get("location")
                if not loc:
                    raise ValueError("redirect without Location")
                cur = urllib.parse.urljoin(cur, loc)
                continue
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}")
            ct = (r.headers.get("content-type")
                  or r.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if not any(ct.startswith(t) for t in _TEXTY_CTS):
                raise ValueError(f"unsupported Content-Type: {ct or 'unknown'}")
            buf = bytearray()
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > _MAX_BYTES:
                    break
            enc = r.encoding or "utf-8"
            try:
                body = bytes(buf).decode(enc, errors="replace")
            except (LookupError, TypeError):
                body = bytes(buf).decode("utf-8", errors="replace")
            return body, ct, cur
        finally:
            try:
                r.close()
            except Exception:
                pass
    raise ValueError("too many redirects")


def _fetch(url):
    try:
        return _fetch_once(url, use_proxy=True)
    except (cc.exceptions.ConnectionError, cc.exceptions.Timeout,
            cc.exceptions.ProxyError) as e:
        # WARP unreachable / unhealthy — retry direct.
        return _fetch_once(url, use_proxy=False)


def fetch_page(url: str) -> str:
    """Fetch an http(s) web page and return its text content.

    Routes through the local WARP SOCKS5 proxy by default so that
    pages blocked on the server's egress IP (reddit, German news
    sites behind anti-bot challenges, etc.) are reachable. Returns
    plain text with HTML stripped, capped at ~8000 characters.
    Returns a string starting with 'error:' on failure. Use this
    instead of WebFetch when WebFetch cannot reach the URL.
    """
    d = _cache_dir()
    _sweep(d)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    cached = os.path.join(d, f"{key}.txt")
    if os.path.exists(cached) and time.time() - os.path.getmtime(cached) < _CACHE_TTL:
        try:
            with open(cached, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            pass

    try:
        body, ct, final_url = _fetch(url)
    except ValueError as e:
        return f"error: {e}"
    except cc.exceptions.RequestException as e:
        return f"error: request failed: {str(e)[:200]}"
    except Exception as e:
        return f"error: {type(e).__name__}: {str(e)[:200]}"

    text = _extract_text(body, ct)
    if not text:
        return "error: page returned no extractable text"
    header = ""
    if final_url and final_url != url:
        header = f"[final URL: {final_url}]\n\n"
    out = header + text
    if len(out) > _MAX_CHARS:
        out = out[:_MAX_CHARS].rsplit(" ", 1)[0] + " …[truncated]"

    try:
        tmp = cached + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(out)
        os.chmod(tmp, 0o600)
        os.replace(tmp, cached)
    except OSError:
        pass
    return out


if __name__ == "__main__":
    os.chdir(tempfile.gettempdir())
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("fetch")
    mcp.tool()(fetch_page)
    mcp.run()
