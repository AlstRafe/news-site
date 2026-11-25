"""The Atom feed of the lists, a visitor's own 後で読む list (/api/articles), the reading time and the
page's way past the header (本文へ移動)."""

import sys
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web import H, WebTestCase

from newsreader.services import translation
from newsreader.web.pages import reading_minutes

ATOM = "{http://www.w3.org/2005/Atom}"
VISITOR = {"headers": {"Host": "sites.duckdns.org"}, "environ_overrides": {"homepage.visitor": True}}


class FeedTests(WebTestCase):
    def feed(self, query=""):
        response = self.client.get("/feed.xml" + query, headers=H)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/atom+xml; charset=utf-8")
        return ET.fromstring(response.get_data())

    def test_the_newest_articles_link_to_their_pages_here(self):
        feed = self.feed()
        self.assertEqual(feed.tag, f"{ATOM}feed")
        self.assertEqual(feed.findtext(f"{ATOM}title"), "NewsBridge")
        self.assertEqual(feed.find(f"{ATOM}link[@rel='self']").get("href"), "http://localhost/feed.xml")
        entries = feed.findall(f"{ATOM}entry")
        self.assertEqual(
            sorted(e.findtext(f"{ATOM}title") for e in entries), ["CNA headline", "NAVER headline", "News Headline"]
        )
        cna = next(e for e in entries if e.findtext(f"{ATOM}title") == "CNA headline")
        self.assertEqual(cna.find(f"{ATOM}link").get("href"), f"http://localhost/a/{self.cna}")
        self.assertEqual(cna.findtext(f"{ATOM}id"), f"http://localhost/a/{self.cna}")
        self.assertEqual(cna.findtext(f"{ATOM}author/{ATOM}name"), "CNA")  # the outlet as the lists name it
        self.assertEqual(cna.findtext(f"{ATOM}summary"), "First CNA paragraph.")
        self.assertRegex(cna.findtext(f"{ATOM}updated"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_the_lists_filters_and_the_lists_link_to_their_feed(self):
        feed = self.feed("?source=CNA")
        self.assertEqual(feed.findtext(f"{ATOM}title"), "NewsBridge · CNA")
        self.assertEqual([e.findtext(f"{ATOM}title") for e in feed.findall(f"{ATOM}entry")], ["CNA headline"])
        self.assertEqual(self.feed("?q=nothing-like-this").findall(f"{ATOM}entry"), [])
        page = self.client.get("/?source=CNA", headers=H).get_data(as_text=True)
        self.assertIn(
            '<link rel="alternate" type="application/atom+xml" title="NewsBridge · CNA" href="/feed.xml?source=CNA">',
            page,
        )
        self.assertIn('<a class="feed-here" href="/feed.xml?source=CNA"', page)
        # every other page offers the whole site's feed
        story_less = self.client.get(f"/a/{self.cna}", headers=H).get_data(as_text=True)
        self.assertIn(
            '<link rel="alternate" type="application/atom+xml" title="NewsBridge" href="/feed.xml">', story_less
        )

    def test_a_headline_already_translated_comes_in_the_readers_language(self):
        # only what was already translated (the list's 見出しを訳す keeps it): the feed asks for nothing
        def kept(titles, target):
            self.assertEqual(target, "ja")
            return ["中央社の見出し" if t == "CNA headline" else "" for t in titles]

        with mock.patch.object(translation, "cached_titles", kept):
            entries = self.feed("?source=CNA").findall(f"{ATOM}entry")
        self.assertEqual(entries[0].findtext(f"{ATOM}title"), "中央社の見出し (CNA headline)")

    def test_visitors_get_it_too_and_crawlers_may_read_it(self):
        response = self.client.get("/feed.xml", **VISITOR)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<entry>", response.get_data())


class VisitorSavedTests(WebTestCase):
    def test_cards_for_the_ids_the_browser_keeps(self):
        other = [i for i in (1, 2, 3) if i != self.cna][0]
        response = self.client.get(f"/api/articles?ids={self.cna},999999,x,{other},{self.cna}", **VISITOR)
        self.assertEqual(response.status_code, 200)
        items = response.get_json()["items"]
        self.assertEqual([i["id"] for i in items], [self.cna, other])  # order kept, unknown and repeated left out
        cna = items[0]
        self.assertEqual(cna["url"], f"/a/{self.cna}")
        self.assertEqual(
            (cna["title"], cna["source"], cna["outlet"], cna["lang"], cna["lang_tag"]),
            ("CNA headline", "CNA", "CNA", "zh", "zh-Hant"),
        )
        self.assertTrue(cna["thumb"].startswith(f"/img/{self.cna}/0?k="))
        self.assertEqual(self.client.get("/api/articles", **VISITOR).get_json(), {"items": []})

    def test_the_visitors_saved_page_and_the_save_button(self):
        page = self.client.get("/saved", **VISITOR).get_data(as_text=True)
        self.assertIn('<div class="card-grid" id="local-saved" aria-busy="true"></div>', page)
        self.assertIn("この端末のブラウザーに保存しているので", page)
        self.assertNotIn("/words", page)  # the 単語帳 stays the owner's
        article = self.client.get(f"/a/{self.cna}", **VISITOR).get_data(as_text=True)
        self.assertIn(f'id="save-article" data-id="{self.cna}" data-local="1"', article)
        owner = self.client.get(f"/a/{self.cna}", headers=H).get_data(as_text=True)
        self.assertIn(f'id="save-article" data-id="{self.cna}" aria-pressed="false"', owner)
        self.assertIn("data-share", owner)


class PageTests(WebTestCase):
    def test_reading_time(self):
        self.assertEqual(reading_minutes([], "en"), 1)
        self.assertEqual(reading_minutes(["word " * 660], "en"), 3)
        self.assertEqual(reading_minutes(["字" * 1200], "zh"), 3)
        self.assertEqual(reading_minutes(["가 " * 1500], "ko"), 3)  # spaces are not read
        self.assertIn("1 分で読めます", self.client.get(f"/a/{self.cna}", headers=H).get_data(as_text=True))

    def test_a_way_past_the_header(self):
        page = self.client.get("/", headers=H).get_data(as_text=True)
        self.assertIn('<body id="top"><a class="skip-link" href="#main">本文へ移動</a>', page)
        self.assertIn('<main class="wrap front-page" id="main" tabindex="-1">', page)
        self.assertIn(
            '<main class="wrap" id="main" tabindex="-1">', self.client.get("/saved", headers=H).get_data(as_text=True)
        )

    def test_the_outlets_page_leads_back_to_the_digest(self):
        page = self.client.get("/outlets", headers=H).get_data(as_text=True)
        self.assertIn('<a class="back" href="/digest">今日のまとめへ</a>', page)
        self.assertNotIn('class="back"', self.client.get("/digest", headers=H).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
