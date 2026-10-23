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


#: Crawlers may read pages (link previews come from them) but not call the APIs or the relay.
ROBOTS = b"User-agent: *\nDisallow: /news/api/\nDisallow: /relay/\n"


def _public_headers(start_response):
    """For the public name: https only from now on, and no search-engine listing (a test, for people sent the link)."""
    def start(status, headers, exc_info=None):
        return start_response(status, [*headers, ("Strict-Transport-Security", "max-age=31536000"),
                                       ("X-Robots-Tag", "noindex")], exc_info)
    return start


class Site:
    """``preview`` (optional) returns what the site has now: {"note": str, "items": [(text, path)]},
    path relative to the site; the index shows it under the description. Errors show nothing."""

    def __init__(self, mount: str, name: str, subtitle: str, description: str, app, preview=None):
        self.mount, self.name, self.subtitle, self.description, self.app = mount, name, subtitle, description, app
        self.preview = preview

    def preview_html(self) -> str:
        try:
            now = self.preview() if self.preview else None
        except Exception:  # noqa: BLE001 - the index never fails because a site's preview did
            now = None
        if not now:
            return ""
        items = "".join(f'<li><a href="{html.escape(self.mount + path)}">{html.escape(text)}</a></li>'
                        for text, path in now.get("items", []))
        note = f'<p class="note">{html.escape(now["note"])}</p>' if now.get("note") else ""
        return note + (f'<ul class="now">{items}</ul>' if items else "")


class Portal:
    """WSGI: "/" is the index, ``/<site>/...`` goes to that site's app."""

    def __init__(self, sites: list[Site], allowed_hosts=None, public_hosts=None):
        self.sites = sites
        self.allowed_hosts = allowed_hosts  # callable(host) -> bool, or None for any
        # callable(host) -> bool: names the public reaches through a TLS proxy (テスト公開). Their
        # links are built as https (share previews need absolute https URLs) and kept out of search results.
        self.public_hosts = public_hosts
        self.page = (_HERE / "index.html").read_text(encoding="utf-8")
        self.css = (_HERE / "portal.css").read_bytes()

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "") or "/"
        method = environ.get("REQUEST_METHOD", "GET")
        if self.public_hosts is not None and self.public_hosts(environ.get("HTTP_HOST", "")):
            environ = dict(environ, **{"wsgi.url_scheme": "https"})
            start_response = _public_headers(start_response)
        for site in self.sites:
            if path == site.mount:
                return self._redirect(start_response, site.mount + "/", environ)
            if path.startswith(site.mount + "/"):
                environ = dict(environ, SCRIPT_NAME=environ.get("SCRIPT_NAME", "") + site.mount,
                               PATH_INFO=path[len(site.mount):])
                return site.app(environ, start_response)
        if path in ("/", "/portal.css", "/robots.txt"):
            if self.allowed_hosts is not None and not self.allowed_hosts(environ.get("HTTP_HOST", "")):
                return self._plain(start_response, "400 Bad Request", b"Bad Request")
            if method not in ("GET", "HEAD"):
                return self._plain(start_response, "405 Method Not Allowed", b"Method Not Allowed")
            if path == "/portal.css":
                return self._send(start_response, "text/css; charset=utf-8", self.css, "public, max-age=3600")
            if path == "/robots.txt":
                return self._send(start_response, "text/plain; charset=utf-8", ROBOTS, "public, max-age=86400")
            return self._send(start_response, "text/html; charset=utf-8", self._index().encode("utf-8"), "no-cache")
        main = self.sites[0]
        if path.startswith(LEGACY_DIRECT) or method not in ("GET", "HEAD"):
            return main.app(environ, start_response)
        return self._redirect(start_response, main.mount + path, environ, permanent=True)

    def _index(self) -> str:
        rows = "\n".join(
            f'<li><span class="no">{n:02d}</span><div><a href="{html.escape(s.mount)}/">{html.escape(s.name)}</a>'
            f'<span class="sub">{html.escape(s.subtitle)}</span>'
            f'<p>{html.escape(s.description)}</p>{s.preview_html()}</div></li>' for n, s in enumerate(self.sites, 1))
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
    from newsreader import branding
    from newsreader.web import public, security
    news = Site(NEWS_MOUNT, branding.APP_NAME, branding.APP_TAGLINE,
                "BBC・中央社・聯合ニュースと、技術系の InfoQ・iThome などを毎時取得。原文と訳を並べ、同じニュースを各社で比べる。",
                news_app, preview=lambda: news_now(news_app))
    trusted = news_app.config["TRUSTED_HOSTS_SET"]
    def allowed(host: str) -> bool:
        host = security.host_without_port(host)
        return host in trusted or public.is_funnel_host(host)  # テスト公開 (web/public.py)
    return Portal([news], allowed_hosts=allowed,
                  public_hosts=lambda host: public.is_funnel_host(security.host_without_port(host)))


#: Headlines the index shows from the news site.
NEWS_PREVIEW = 3


def news_now(news_app) -> dict:
    """The news site's latest stories several outlets carried (or its newest articles), in Japanese
    where the headline was translated already, and when it last collected."""
    from newsreader.library import index, status
    from newsreader.services import translation
    from newsreader.web.pages import front_stories
    db = news_app.config["LIBRARY_DB"]
    with news_app.test_request_context("/"):  # front_stories builds picture URLs
        stories = front_stories(db)[:NEWS_PREVIEW]
        if stories:
            rows = [(s["lead_title"], f"/stories/{quote(s['key'])}") for s in stories]
        else:
            rows = [(a["title"], f"/a/{a['id']}") for a in index.list_articles(db, page=1, per_page=NEWS_PREVIEW)["items"]]
        kept = translation.cached_titles([title for title, _ in rows], "ja")
        from newsreader.services.settings import collects_hourly
        from newsreader.web.status_view import status_data
        badge = status.badge(status_data(), collects=collects_hourly())
    return {"note": badge["label"] if badge.get("tone") != "idle" else "",
            "items": [(ja or title, path) for (title, path), ja in zip(rows, kept)]}
