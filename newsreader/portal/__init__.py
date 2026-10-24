"""News Site served on its own (app.py): the site under ``/news``, and "/" leading there.

On this PC the front page of the PC's sites ("/", News Site among them, CoreLab under ``/infra``)
belongs to the Home Page project, whose server hosts News Site through newsreader.hosting. This
module is for News Site run by itself: another PC, a test build, or this PC when the Home Page
server is not running.

Addresses from before the mount keep working: a page address (``/a/123``, a bookmark) is
redirected to the same page under ``/news``; the translation relay (``/relay/...``, called by
test builds with a fixed address) is answered where it always was. Requests under the public
name (テスト公開) are https and kept out of search results.
"""
from __future__ import annotations

from urllib.parse import quote

from newsreader.hosting import DISALLOW, LEGACY_DIRECT

NEWS_MOUNT = "/news"
#: Crawlers may read pages (link previews come from them) but not call the APIs or the relay.
ROBOTS = ("User-agent: *\n" + "".join(f"Disallow: {NEWS_MOUNT}{part}\n" for part in DISALLOW)
          + "".join(f"Disallow: {part}\n" for part in LEGACY_DIRECT)).encode()


def _public_headers(start_response):
    """For the public name: https only from now on, and no search-engine listing (a test, for people sent the link)."""
    def start(status, headers, exc_info=None):
        return start_response(status, [*headers, ("Strict-Transport-Security", "max-age=31536000"),
                                       ("X-Robots-Tag", "noindex")], exc_info)
    return start


class NewsOnly:
    """WSGI: News Site under /news; "/" and the old addresses lead there."""

    def __init__(self, news_app, public_hosts=None):
        self.news_app = news_app
        self.public_hosts = public_hosts  # callable(host) -> bool: names reached through a TLS proxy (Funnel)

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "") or "/"
        method = environ.get("REQUEST_METHOD", "GET")
        if self.public_hosts is not None and self.public_hosts(environ.get("HTTP_HOST", "")):
            environ = dict(environ, **{"wsgi.url_scheme": "https"})
            start_response = _public_headers(start_response)
        if path.startswith(NEWS_MOUNT + "/"):
            environ = dict(environ, SCRIPT_NAME=environ.get("SCRIPT_NAME", "") + NEWS_MOUNT,
                           PATH_INFO=path[len(NEWS_MOUNT):])
            return self.news_app(environ, start_response)
        if path == "/robots.txt" and method in ("GET", "HEAD"):
            start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(ROBOTS))),
                                      ("Cache-Control", "public, max-age=86400")])
            return [ROBOTS]
        if path.startswith(LEGACY_DIRECT) or method not in ("GET", "HEAD"):
            return self.news_app(environ, start_response)
        if path in ("/", NEWS_MOUNT):
            return _redirect(start_response, NEWS_MOUNT + "/", environ)
        return _redirect(start_response, NEWS_MOUNT + path, environ, permanent=True)


def _redirect(start_response, target: str, environ, permanent: bool = False):
    query = environ.get("QUERY_STRING", "")
    location = quote(target, safe="/%:@!$&'()*+,;=-._~") + (f"?{query}" if query else "")
    start_response("308 Permanent Redirect" if permanent else "302 Found",
                   [("Location", location), ("Content-Length", "0")])
    return [b""]


def build(news_app) -> NewsOnly:
    """What app.py serves: News Site alone."""
    from newsreader.web import public, security
    return NewsOnly(news_app, public_hosts=lambda host: public.is_funnel_host(security.host_without_port(host)))
