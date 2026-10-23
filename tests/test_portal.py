"""The front page ("/") and News Site under /news."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from werkzeug.test import Client

from newsreader import portal
from newsreader.library import index
from newsreader.web import create_app

H = {"Host": "localhost"}


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        root = self.tmp / "articles" / "BBC"
        root.mkdir(parents=True)
        (root / "a.html").write_bytes(b'''<html><head><meta name="pagetype" content="News Article">
<meta property="article:published_time" content="2026-10-13T10:00:00Z"></head><body><main id="bbc-main">
<h1>Heavy snow closes Alpine passes</h1><article><div data-component="layout-block">
<p class="Paragraph-styles__ParagraphStyled-x">Snow.</p></div></article></main></body></html>''')
        db = self.tmp / "library.sqlite3"
        index._last_sync.clear()
        index.sync(self.tmp / "articles", db, force=True)
        self.article = index.list_articles(db)["items"][0]["id"]
        self.app = create_app(self.tmp / "articles", db, start_background=False, secret_key=b"k" * 32, mount="/news")
        self.client = Client(portal.build(self.app))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_front_page_lists_the_sites(self):
        response = self.client.get("/", headers=H)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('<a href="/news/">NewsBridge</a>', page)
        # what the news site has now: its newest headlines, linked under /news
        self.assertIn('<ul class="now">', page)
        self.assertIn(f'href="/news/a/{self.article}"', page)
        self.assertNotIn("{{", page)
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(self.client.get("/portal.css", headers=H).status_code, 200)
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(self.client.post("/", headers=H).status_code, 405)

    def test_news_site_under_its_path(self):
        page = self.client.get("/news/", headers=H).get_data(as_text=True)
        self.assertIn('<meta name="app-base" content="/news">', page)
        self.assertIn(f'href="/news/a/{self.article}"', page)
        self.assertIn('"/news/a/*"', page)  # prefetch rules
        self.assertIn("/news/static/style.css", page)
        self.assertEqual(self.client.get(f"/news/a/{self.article}", headers=H).status_code, 200)
        response = self.client.get("/news", headers=H)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/news/"))

    def test_the_public_name_reads_and_translates_but_changes_nothing(self):
        funnel = {"Host": "haruxera.tail1234.ts.net"}
        self.assertEqual(self.client.post("/relay/v1/ping", headers={**funnel, "Authorization": "Bearer nbr_x"}).status_code, 401)
        self.assertEqual(self.client.get("/", headers=funnel).status_code, 200)
        page = self.client.get("/news/", headers=funnel)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertNotIn("/news/translation-settings", html)
        self.assertNotIn("/news/saved", html)
        self.assertEqual(self.client.get(f"/news/a/{self.article}", headers=funnel).status_code, 200)
        for owner_page in ("/news/translation-settings", "/news/status", "/news/saved", "/news/words"):
            self.assertEqual(self.client.get(owner_page, headers=funnel).status_code, 404, owner_page)
        import re
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        self.assertEqual(self.client.post("/news/api/saved", json={"id": self.article, "saved": True},
                                          headers={**funnel, "X-CSRF-Token": csrf}).status_code, 403)
        # the owner's own address still has everything, and the visit left no 既読 mark
        self.assertEqual(self.client.get("/news/translation-settings", headers=H).status_code, 200)
        from newsreader.library import reading
        self.assertEqual(reading.states(reading.path_for(self.tmp / "library.sqlite3"),
                                        index.list_articles(self.tmp / "library.sqlite3")["items"]), {})
        self.assertEqual(self.client.post("/relay/v1/ping", headers={"Host": "evil.example", "Authorization": "Bearer nbr_x"}).status_code, 403)

    def test_the_public_name_is_https_and_kept_out_of_search(self):
        funnel = {"Host": "haruxera.tail1234.ts.net"}
        response = self.client.get(f"/news/a/{self.article}", headers=funnel)
        html = response.get_data(as_text=True)
        # share previews need absolute https addresses (Funnel ends TLS in front of the app)
        self.assertIn(f'<meta property="og:url" content="https://haruxera.tail1234.ts.net/news/a/{self.article}">', html)
        self.assertIn('content="https://haruxera.tail1234.ts.net/news/static/icon-512.png', html)
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex")
        self.assertIn("max-age=", response.headers["Strict-Transport-Security"])
        self.assertIn("Secure", response.headers["Set-Cookie"])
        self.assertEqual(self.client.get("/", headers=funnel).headers["X-Robots-Tag"], "noindex")
        robots = self.client.get("/robots.txt", headers=funnel)
        self.assertEqual(robots.status_code, 200)
        self.assertIn("Disallow: /news/api/", robots.get_data(as_text=True))
        # the owner's own address: plain http, no such headers
        local = self.client.get(f"/news/a/{self.article}", headers=H)
        self.assertIn('content="http://localhost/news/a/', local.get_data(as_text=True))
        self.assertNotIn("X-Robots-Tag", local.headers)
        self.assertNotIn("Secure", local.headers["Set-Cookie"])

    def test_error_pages_have_a_heading(self):
        page = self.client.get("/news/a/999999", headers=H).get_data(as_text=True)
        self.assertIn("<h1>ページが見つかりません</h1>", page)

    def test_a_visitors_translations_count_as_automatic(self):
        from newsreader.services import translation
        from newsreader.web import public
        flask_app = self.app
        with flask_app.test_request_context("/api/translate", base_url="https://haruxera.tail1234.ts.net"):
            self.assertTrue(public.is_visitor())
        with flask_app.test_request_context("/api/translate", base_url="http://localhost"):
            self.assertFalse(public.is_visitor())
        token = translation.visitor_translations.set(True)
        try:
            with unittest.mock.patch("newsreader.services.cache.monthly_translation_usage",
                                     return_value={"providers": {"azure": {"metered_chars": translation.AUTO_MONTHLY_CHARS}}}):
                with self.assertRaises(translation.TranslationError):
                    translation._check_automatic()
        finally:
            translation.visitor_translations.reset(token)

    def test_old_addresses(self):
        response = self.client.get(f"/a/{self.article}?tr=ja", headers=H)
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers["Location"], f"/news/a/{self.article}?tr=ja")
        # the relay keeps its address (test builds call it with POST and a fixed URL)
        self.assertEqual(self.client.post("/relay/v1/ping", headers=H).status_code,
                         Client(self.app).post("/relay/v1/ping", headers=H).status_code)
