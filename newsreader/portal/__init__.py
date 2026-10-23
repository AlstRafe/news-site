"""The front page of this PC's sites: "/" lists the sites, each site lives under its own path.

News Site (newsreader.web) is mounted at ``/news`` (it builds every link with ``url_for``, so it
only needs ``SCRIPT_NAME``). Addresses from before the mount keep working: a page address
(``/a/123``, a bookmark) is redirected to the same page under ``/news``; the translation relay
(``/relay/...``, called by test builds with a fixed address) is answered where it always was.
"""
from __future__ import annotations

import html
from pathlib import Path
from urllib.parse import quote

NEWS_MOUNT = "/news"
#: Answered at the old address too (POST from test builds; urllib does not follow a redirect for it).
LEGACY_DIRECT = ("/relay/",)
_HERE = Path(__file__).resolve().parent


class Site:
    def __init__(self, mount: str, name: str, subtitle: str, description: str, app):
        self.mount, self.name, self.subtitle, self.description, self.app = mount, name, subtitle, description, app


class Portal:
    """WSGI: "/" is the index, ``/<site>/...`` goes to that site's app."""

    def __init__(self, sites: list[Site], allowed_hosts=None):
        self.sites = sites
        self.allowed_hosts = allowed_hosts  # callable(host) -> bool, or None for any
        self.page = (_HERE / "index.html").read_text(encoding="utf-8")
        self.css = (_HERE / "portal.css").read_bytes()

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "") or "/"
        method = environ.get("REQUEST_METHOD", "GET")
        for site in self.sites:
            if path == site.mount:
                return self._redirect(start_response, site.mount + "/", environ)
            if path.startswith(site.mount + "/"):
                environ = dict(environ, SCRIPT_NAME=environ.get("SCRIPT_NAME", "") + site.mount,
                               PATH_INFO=path[len(site.mount):])
                return site.app(environ, start_response)
        if path in ("/", "/portal.css"):
            if self.allowed_hosts is not None and not self.allowed_hosts(environ.get("HTTP_HOST", "")):
                return self._plain(start_response, "400 Bad Request", b"Bad Request")
            if method not in ("GET", "HEAD"):
                return self._plain(start_response, "405 Method Not Allowed", b"Method Not Allowed")
            if path == "/portal.css":
                return self._send(start_response, "text/css; charset=utf-8", self.css, "public, max-age=3600")
            return self._send(start_response, "text/html; charset=utf-8", self._index().encode("utf-8"), "no-cache")
        main = self.sites[0]
        if path.startswith(LEGACY_DIRECT) or method not in ("GET", "HEAD"):
            return main.app(environ, start_response)
        return self._redirect(start_response, main.mount + path, environ, permanent=True)

    def _index(self) -> str:
        rows = "\n".join(
            f'<li><span class="no">{n:02d}</span><div><a href="{html.escape(s.mount)}/">{html.escape(s.name)}</a>'
            f'<span class="sub">{html.escape(s.subtitle)}</span>'
            f'<p>{html.escape(s.description)}</p></div></li>' for n, s in enumerate(self.sites, 1))
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone(timedelta(hours=9)))
        date = f"{today.year}年{today.month}月{today.day}日（{'月火水木金土日'[today.weekday()]}）"
        return self.page.replace("{{ sites }}", rows).replace("{{ date }}", date).replace("{{ count }}", str(len(self.sites)))

    @staticmethod
    def _headers(content_type: str, length: int, cache: str) -> list[tuple[str, str]]:
        return [("Content-Type", content_type), ("Content-Length", str(length)), ("Cache-Control", cache),
                ("Content-Security-Policy", "default-src 'none'; style-src 'self'; img-src 'self'; "
                                            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
                ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin")]

    def _send(self, start_response, content_type: str, body: bytes, cache: str):
        start_response("200 OK", self._headers(content_type, len(body), cache))
        return [body]

    @staticmethod
    def _plain(start_response, status: str, body: bytes):
        start_response(status, [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))])
        return [body]

    @staticmethod
    def _redirect(start_response, target: str, environ, permanent: bool = False):
        query = environ.get("QUERY_STRING", "")
        location = quote(target, safe="/%:@!$&'()*+,;=-._~") + (f"?{query}" if query else "")
        start_response("308 Permanent Redirect" if permanent else "302 Found",
                       [("Location", location), ("Content-Length", "0")])
        return [b""]


def build(news_app) -> Portal:
    """The sites of this PC (one for now) behind one address."""
    from newsreader.web import security
    news = Site(NEWS_MOUNT, "News Site", "ニュースを訳して読み比べる",
                "BBC・中央社・聯合ニュースと、技術系の InfoQ・iThome などを毎時取得。原文と訳を並べ、同じニュースを各社で比べる。", news_app)
    trusted = news_app.config["TRUSTED_HOSTS_SET"]
    return Portal([news], allowed_hosts=lambda host: security.host_without_port(host) in trusted)
