"""News Site under /news: served on its own (app.py), and as a site of the Home Page server (newsreader.hosting)."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from werkzeug.test import Client

from newsreader import hosting, portal
from newsreader.library import index
from newsreader.web import create_app

H = {"Host": "localhost"}


class Library(unittest.TestCase):
    """One article, and News Site over it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        root = self.tmp / "articles" / "BBC"
        root.mkdir(parents=True)
        (root / "a.html").write_bytes(b"""<html><head><meta name="pagetype" content="News Article">
<meta property="article:published_time" content="2026-10-13T10:00:00Z"></head><body><main id="bbc-main">
<h1>Heavy snow closes Alpine passes</h1><article><div data-component="layout-block">
<p class="Paragraph-styles__ParagraphStyled-x">Snow.</p></div></article></main></body></html>""")
        db = self.tmp / "library.sqlite3"
        index._last_sync.clear()
        index.sync(self.tmp / "articles", db, force=True)
        self.article = index.list_articles(db)["items"][0]["id"]
        self.app = create_app(self.tmp / "articles", db, start_background=False, secret_key=b"k" * 32, mount="/news")
        self.client = Client(portal.build(self.app))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class PortalTests(Library):
    def test_on_its_own_the_front_page_is_news_site(self):
        # (on this PC the Home Page server owns "/" and lists News Site with the other sites)
        response = self.client.get("/", headers=H)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/news/"))
        robots = self.client.get("/robots.txt", headers=H).get_data(as_text=True)
        self.assertEqual(robots, "User-agent: *\nDisallow: /news/api/\nDisallow: /relay/\n")

    def test_news_site_under_its_path(self):
        page = self.client.get("/news/", headers=H).get_data(as_text=True)
        self.assertIn('<meta name="app-base" content="/news">', page)
        self.assertIn(f'href="/news/a/{self.article}"', page)
        self.assertIn('"/news/a/*"', page)  # prefetch rules
        self.assertIn("/news/static/style.css", page)
        # the way back to the site list, always in reach as on the PC's other sites (Home Page CONVENTIONS): the
        # header's first link (the header stays at the top), on phones the bottom tab bar's first tab; and the footer
        arrow = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5M11 5.5 4.5 12l6.5 6.5"/></svg>'
        back = f'href="/" title="サイト一覧へ戻る" aria-label="サイト一覧へ戻る">{arrow}<span>サイト一覧</span></a>'
        self.assertIn(f'<div class="wrap brand"><a class="portal-link" {back}<a class="brand-link"', page)
        self.assertIn(
            f'<nav class="tabbar with-home" aria-label="メインメニュー（下）"><a class="tab-home" {back}', page
        )
        self.assertIn('<a href="/">← サイト一覧</a>', page)
        css = self.client.get("/news/static/product.css", headers=H).get_data(as_text=True)
        self.assertRegex(css, r"@media \(min-width: 651px\) \{\n  \.topbar \{\n    position: sticky;\n    top: 0;")
        self.assertRegex(css, r"\n  \.portal-link \{\n    display: none;\n  \}")
        self.assertEqual(self.client.get(f"/news/a/{self.article}", headers=H).status_code, 200)
        response = self.client.get("/news", headers=H)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/news/"))

    def test_the_public_name_reads_and_translates_but_changes_nothing(self):
        funnel = {"Host": "haruxera.tail1234.ts.net"}
        self.assertEqual(
            self.client.post("/relay/v1/ping", headers={**funnel, "Authorization": "Bearer nbr_x"}).status_code, 401
        )
        self.assertEqual(self.client.get("/", headers=funnel).headers["Location"], "/news/")
        page = self.client.get("/news/", headers=funnel)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertNotIn("/news/translation-settings", html)
        self.assertNotIn("/news/words", html)
        self.assertIn('href="/news/saved"', html)  # 保存 is theirs too, kept in their browser
        self.assertEqual(self.client.get(f"/news/a/{self.article}", headers=funnel).status_code, 200)
        for owner_page in ("/news/translation-settings", "/news/status", "/news/words"):
            self.assertEqual(self.client.get(owner_page, headers=funnel).status_code, 404, owner_page)
        import re

        csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        self.assertEqual(
            self.client.post(
                "/news/api/saved", json={"id": self.article, "saved": True}, headers={**funnel, "X-CSRF-Token": csrf}
            ).status_code,
            403,
        )
        # the owner's own address still has everything, and the visit left no 既読 mark
        self.assertEqual(self.client.get("/news/translation-settings", headers=H).status_code, 200)
        from newsreader.library import reading

        self.assertEqual(
            reading.states(
                reading.path_for(self.tmp / "library.sqlite3"),
                index.list_articles(self.tmp / "library.sqlite3")["items"],
            ),
            {},
        )
        self.assertEqual(
            self.client.post(
                "/relay/v1/ping", headers={"Host": "evil.example", "Authorization": "Bearer nbr_x"}
            ).status_code,
            403,
        )

    def test_the_public_name_is_https_and_kept_out_of_search(self):
        funnel = {"Host": "haruxera.tail1234.ts.net"}
        response = self.client.get(f"/news/a/{self.article}", headers=funnel)
        html = response.get_data(as_text=True)
        # share previews need absolute https addresses (Funnel ends TLS in front of the app)
        self.assertIn(
            f'<meta property="og:url" content="https://haruxera.tail1234.ts.net/news/a/{self.article}">', html
        )
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
        self.assertIn('<a class="tab-home" href="/"', page)  # the way back is on every page, error pages too

    def test_without_the_site_list_there_is_no_way_back_to_it(self):
        # (served at the root by itself: a distributed build, a test server)
        app = create_app(
            self.tmp / "articles", self.tmp / "library.sqlite3", start_background=False, secret_key=b"k" * 32
        )
        page = Client(app).get("/", headers=H).get_data(as_text=True)
        self.assertNotIn("portal-link", page)
        self.assertNotIn("tab-home", page)
        self.assertIn('<nav class="tabbar" aria-label="メインメニュー（下）"><a href="/"', page)

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
            with unittest.mock.patch(
                "newsreader.services.cache.monthly_translation_usage",
                return_value={"providers": {"azure": {"metered_chars": translation.AUTO_MONTHLY_CHARS}}},
            ):
                with self.assertRaises(translation.TranslationError):
                    translation._check_automatic()
        finally:
            translation.visitor_translations.reset(token)

    def test_old_addresses(self):
        response = self.client.get(f"/a/{self.article}?tr=ja", headers=H)
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers["Location"], f"/news/a/{self.article}?tr=ja")
        # the relay keeps its address (test builds call it with POST and a fixed URL)
        self.assertEqual(
            self.client.post("/relay/v1/ping", headers=H).status_code,
            Client(self.app).post("/relay/v1/ping", headers=H).status_code,
        )


class HostingTests(Library):
    """What the Home Page server gets from newsreader.hosting.site, and how News Site steps aside for it."""

    def test_site_for_a_host(self):
        host = {
            "port": 5000,
            "addresses": ["127.0.0.1", "100.70.1.2"],
            "names": ["100.70.1.2", "haruxera"],
            "urls": ["http://127.0.0.1:5000/", "http://100.70.1.2:5000/"],
        }
        with (
            unittest.mock.patch("newsreader.web.create_app", return_value=self.app) as factory,
            unittest.mock.patch.object(hosting, "_hand_over_the_start") as hand_over,
            unittest.mock.patch.dict("os.environ", {"NEWS_ALLOWED_HOSTS": "example.lan"}),
        ):
            made = hosting.site("/news", host)
            import os

            self.assertEqual(os.environ["NEWS_ALLOWED_HOSTS"], "example.lan,100.70.1.2,haruxera")
        factory.assert_called_once_with(mount="/news")
        hand_over.assert_called_once_with(host, "/news")
        self.assertIs(made["app"], self.app)
        self.assertEqual((made["name"], made["disallow"], made["legacy"]), ("NewsBridge", ("/api/",), ("/relay/",)))
        now = made["preview"]()
        self.assertEqual(now["title"], "新着記事")  # no 読み比べ groups in this library yet
        self.assertEqual(now["items"][0][1], f"/a/{self.article}")
        self.assertIsInstance(made["version"](), str)

    def test_the_front_pages_preview_puts_translated_headlines_first(self):
        stories = [{"lead_title": f"Headline {n}", "key": f"s{n}"} for n in range(1, 6)]
        kept = {"Headline 2": "見出し2", "Headline 4": "見出し4"}
        with (
            unittest.mock.patch("newsreader.web.pages.front_stories", return_value=stories),
            unittest.mock.patch(
                "newsreader.services.translation.cached_titles",
                side_effect=lambda titles, target: [kept.get(t, "") for t in titles],
            ),
        ):
            now = hosting.news_now(self.app)
        self.assertEqual(now["title"], "各社が伝えた話題")
        self.assertEqual(
            now["items"], [("見出し2", "/stories/s2"), ("見出し4", "/stories/s4"), ("Headline 1", "/stories/s1")]
        )

    def test_the_host_takes_over_the_start_at_logon(self):
        from newsreader.services import settings

        with (
            unittest.mock.patch.object(hosting, "HOSTED_PATH", self.tmp / "instance" / "hosted.json"),
            unittest.mock.patch.object(settings, "SETTINGS_PATH", self.tmp / "settings.json"),
            unittest.mock.patch.object(hosting, "_remove_logon_task") as remove,
        ):
            hosting._hand_over_the_start({"port": 5000, "urls": ["http://127.0.0.1:5000/"]}, "/news")
            self.assertEqual(hosting.hosted(), {"by": "Home Page", "url": "http://127.0.0.1:5000/news/", "port": 5000})
            self.assertFalse(settings.start_at_logon())
        remove.assert_called_once()

    def test_the_launcher_opens_the_hosted_site_instead_of_starting_another(self):
        import importlib.util
        import json
        import socket

        spec = importlib.util.spec_from_file_location(
            "launcher_under_test", Path(__file__).resolve().parents[1] / "launcher.py"
        )
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        record = self.tmp / "hosted.json"
        with socket.socket() as listening, unittest.mock.patch.object(launcher, "HOSTED_PATH", record):
            self.assertEqual(launcher.hosted_url(), "")  # nothing hosts it
            listening.bind(("127.0.0.1", 0))
            listening.listen()
            port = listening.getsockname()[1]
            record.write_text(json.dumps({"url": "http://127.0.0.1:5000/news/", "port": port}), encoding="utf-8")
            self.assertEqual(launcher.hosted_url(), "http://127.0.0.1:5000/news/")
            listening.close()
            self.assertEqual(launcher.hosted_url(), "")  # the host is not running: start as before
