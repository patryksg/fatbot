#!/usr/bin/env python3
"""MCP server exposing a single tool: view_image(url).

Downloads an image from an http(s) URL into a cache directory under
XDG_CACHE_HOME and returns the local path so the calling model can Read
it as vision content. SSRF-guarded (rejects non-global IPs incl. CGNAT),
size-capped, Content-Type-restricted to image/*, follows up to 5
redirects revalidating the host on each hop.

Stdio MCP server, intended to be spawned by `claude` via --mcp-config.
"""

import hashlib
import ipaddress
import os
import socket
import tempfile
import time
import urllib.parse

# FastMCP's pydantic-settings tries to read `.env` from CWD on import.
# Move to a directory we can read but that has no .env file.
os.chdir(tempfile.gettempdir())

from curl_cffi import requests as cc
from mcp.server.fastmcp import FastMCP


_IMPERSONATE = "chrome131"
_MAX_HOPS = 5
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_TIMEOUT = 8.0
_CACHE_TTL = 3600  # 1h, files older than this are swept on each call

_EXT_BY_CT = {
    "image/jpeg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/tiff": ".tiff",
    "image/heic": ".heic",
    "image/avif": ".avif",
}


def _cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "claude-images")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _ip_is_safe(ip):
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in ipaddress.ip_network("100.64.0.0/10"):  # CGNAT
            return False
    return True


def _resolve_safe(host):
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    if not infos:
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            return None
        if not _ip_is_safe(ip):
            return None
    return infos[0][4][0]


_session = None


def _get_session():
    global _session
    if _session is None:
        _session = cc.Session(impersonate=_IMPERSONATE)
    return _session


def _sweep_cache(d):
    now = time.time()
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if now - os.path.getmtime(p) > _CACHE_TTL:
                    os.unlink(p)
            except OSError:
                pass
    except OSError:
        pass


def _fetch(url):
    """Walk redirects, revalidating host on each hop. Returns
    (final_response_with_body, content_type) or raises ValueError."""
    visited = set()
    cur = url
    sess = _get_session()
    for _ in range(_MAX_HOPS + 1):
        if cur in visited:
            raise ValueError("redirect loop")
        visited.add(cur)
        parsed = urllib.parse.urlsplit(cur)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("only http(s) URLs are accepted")
        if _resolve_safe(parsed.hostname or "") is None:
            raise ValueError("host resolves to a non-public address")
        r = sess.get(
            cur,
            timeout=_TIMEOUT,
            allow_redirects=False,
            stream=True,
            headers={
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Sec-Fetch-Dest": "image",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Site": "cross-site",
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
            if not ct.startswith("image/"):
                raise ValueError(f"not an image (Content-Type: {ct or 'unknown'})")
            buf = bytearray()
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > _MAX_BYTES:
                    raise ValueError(f"image exceeds {_MAX_BYTES} bytes")
            return bytes(buf), ct
        finally:
            try:
                r.close()
            except Exception:
                pass
    raise ValueError("too many redirects")


mcp = FastMCP("imageview")


@mcp.tool()
def view_image(url: str) -> str:
    """Download an image from an http(s) URL and return its local path.

    The returned path can be opened with the Read tool to view the image
    as vision content. Only image/* Content-Types are accepted; size is
    capped at 10 MB; private/loopback/CGNAT addresses are rejected.
    """
    d = _cache_dir()
    _sweep_cache(d)
    body, ct = _fetch(url)
    ext = _EXT_BY_CT.get(ct, ".img")
    h = hashlib.sha256(body).hexdigest()[:24]
    path = os.path.join(d, f"{h}{ext}")
    if not os.path.exists(path):
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    return path


if __name__ == "__main__":
    mcp.run()
