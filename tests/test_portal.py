"""The front page ("/") and News Site under /news."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
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
        self.assertIn('<a href="/news/">News Site</a>', page)
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

    def test_old_addresses(self):
        response = self.client.get(f"/a/{self.article}?tr=ja", headers=H)
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers["Location"], f"/news/a/{self.article}?tr=ja")
        # the relay keeps its address (test builds call it with POST and a fixed URL)
        self.assertEqual(self.client.post("/relay/v1/ping", headers=H).status_code,
                         Client(self.app).post("/relay/v1/ping", headers=H).status_code)
