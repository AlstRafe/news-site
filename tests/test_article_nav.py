"""End of an article: the next (older) and previous (newer) article of the same outlet."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from newsreader.library import index as library
from newsreader.web import create_app
from test_extractor import CNA, NORMAL


class ArticleNavigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "articles"
        for site, name, data in [("BBC", "a.html", NORMAL), ("CNA", "c.html", CNA)]:
            (self.root / site).mkdir(parents=True, exist_ok=True)
            (self.root / site / name).write_bytes(data)
        self.db = self.tmp / "library.sqlite3"
        library._last_sync.clear()
        self.client = create_app(self.root, self.db, start_background=False, secret_key=b"k" * 32).test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_article_end_links_the_next_and_previous_article_of_the_outlet(self):
        for n, stamp in ((1, 1_700_000_300), (2, 1_700_000_200), (3, 1_700_000_100)):
            path = self.root / "BBC" / f"n{n}.html"
            path.write_bytes(NORMAL.replace(b"News Headline", f"Story {n}".encode()))
            os.utime(path, (stamp, stamp))
        os.utime(self.root / "BBC" / "a.html", (1_600_000_000, 1_600_000_000))  # the oldest BBC article
        library.sync(self.root, self.db, force=True)
        order = [i["id"] for i in library.list_articles(self.db, source="BBC")["items"]]
        self.assertEqual(len(order), 4)
        middle = library.neighbours(self.db, order[1])
        self.assertEqual((middle["newer"]["id"], middle["older"]["id"]), (order[0], order[2]))
        self.assertIsNone(library.neighbours(self.db, order[0])["newer"])
        self.assertIsNone(library.neighbours(self.db, order[-1])["older"])
        self.assertEqual(library.neighbours(self.db, 999999), {"newer": None, "older": None})
        cna = library.list_articles(self.db, source="CNA")["items"][0]["id"]
        self.assertEqual(library.neighbours(self.db, cna), {"newer": None, "older": None})  # only one CNA article
        html = self.client.get(f"/a/{order[1]}", headers={"Host": "localhost"}).get_data(as_text=True)
        self.assertIn(f'href="/a/{order[2]}" rel="next"', html)
        self.assertIn(f'href="/a/{order[0]}" rel="prev"', html)
        self.assertNotIn('class="next-articles"', self.client.get(f"/a/{cna}", headers={"Host": "localhost"}).get_data(as_text=True))




class OriginalLanguageTests(unittest.TestCase):
    """Original text carries its language (Traditional Chinese glyphs, Korean line breaking)."""
    setUp = ArticleNavigationTests.setUp
    tearDown = ArticleNavigationTests.tearDown

    def test_original_text_is_marked_with_its_language(self):
        library.sync(self.root, self.db, force=True)
        items = {i["source"]: i["id"] for i in library.list_articles(self.db)["items"]}
        home = self.client.get("/", headers={"Host": "localhost"}).get_data(as_text=True)
        self.assertIn('<h2 lang="zh-Hant">', home)
        self.assertIn('<h2 lang="en">', home)
        page = self.client.get(f"/a/{items['CNA']}", headers={"Host": "localhost"}).get_data(as_text=True)
        self.assertIn('<h1 lang="zh-Hant">', page)
        self.assertIn('<p lang="zh-Hant">', page)
        self.assertIn('<html lang="ja">', page)  # the interface itself stays Japanese


if __name__ == "__main__":
    unittest.main()


class DaySeparatorTests(unittest.TestCase):
    def test_local_day_labels(self):
        from datetime import datetime, timezone
        from newsreader.web.assets import localday_filter
        stamp = "2026-10-19T03:00:00Z"
        local = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()
        self.assertTrue(localday_filter(stamp, today=local).startswith("今日 · "))
        from datetime import timedelta
        self.assertTrue(localday_filter(stamp, today=local + timedelta(days=1)).startswith("昨日 · "))
        label = localday_filter(stamp, today=local + timedelta(days=5))
        self.assertEqual(label, f"{local.month}月{local.day}日（{'月火水木金土日'[local.weekday()]}）")
        self.assertEqual(localday_filter(""), "")


class VersionTests(unittest.TestCase):
    def test_pages_and_api_carry_the_served_version(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "articles").mkdir()
            app = create_app(tmp / "articles", tmp / "library.sqlite3", start_background=False, secret_key=b"v" * 32)
            client = app.test_client()
            version = app.config["APP_VERSION"]
            reply = client.get("/api/version", headers={"Host": "localhost"})
            self.assertEqual(reply.get_json(), {"version": version})
            self.assertEqual(reply.headers["Cache-Control"], "no-store")
            self.assertIn(f'<meta name="app-version" content="{version}">', client.get("/", headers={"Host": "localhost"}).get_data(as_text=True))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
