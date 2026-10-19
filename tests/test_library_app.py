import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.web import create_app
from newsreader.library import index as library
from test_extractor import CNA, NAVER, NORMAL


class LibraryAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "articles"
        for site, name, data in [("BBC", "a.html", NORMAL), ("NAVER NEWS", "b.html", NAVER), ("CNA", "c.html", CNA)]:
            (self.root / site).mkdir(parents=True, exist_ok=True)
            (self.root / site / name).write_bytes(data)
        (self.root / "BBC" / "notes.txt").write_text("not html")
        self.db = self.tmp / "library.sqlite3"
        library._last_sync.clear()
        # Phase 4: the app is built per test (no module globals); pages read the index only.
        self.client = create_app(self.root, self.db, start_background=False, secret_key=b"k" * 32).test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sync_is_incremental_and_tracks_removal(self):
        first = library.sync(self.root, self.db, force=True)
        self.assertEqual(first["added"], 3)
        again = library.sync(self.root, self.db, force=True)
        self.assertEqual((again["added"], again["updated"], again["removed"]), (0, 0, 0))
        (self.root / "CNA" / "c.html").unlink()
        self.assertEqual(library.sync(self.root, self.db, force=True)["removed"], 1)

    def test_home_lists_sources_and_filters(self):
        library.sync(self.root, self.db, force=True)  # Phase 4 (D-04): indexing happens off the request path
        page = self.client.get("/", headers={"Host": "localhost"})
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        for text in ("News Headline", "NAVER headline", "CNA headline", "3 件"):
            self.assertIn(text, html)
        self.assertNotIn("notes", html)
        only = self.client.get("/?source=CNA", headers={"Host": "localhost"}).get_data(as_text=True)
        self.assertIn("CNA headline", only)
        self.assertNotIn("News Headline", only)
        found = self.client.get("/?q=Naver", headers={"Host": "localhost"}).get_data(as_text=True)
        self.assertIn("NAVER headline", found)
        self.assertIn("1 件", found)

    def test_article_page_and_legacy_link(self):
        library.sync(self.root, self.db, force=True)
        record = library.find_by_file(self.db, "CNA", "c.html")
        page = self.client.get(f"/a/{record['id']}", headers={"Host": "localhost"})
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("First CNA paragraph.", html)
        self.assertIn(f'data-article-id="{record["id"]}"', html)
        old = self.client.get("/article?file=c.html&path=CNA", headers={"Host": "localhost"})
        self.assertEqual(old.status_code, 302)
        self.assertTrue(old.headers["Location"].endswith(f"/a/{record['id']}"))
        self.assertEqual(self.client.get("/a/9999", headers={"Host": "localhost"}).status_code, 404)

    def test_article_end_links_the_next_and_previous_article_of_the_outlet(self):
        import os
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

    def test_path_traversal_is_refused(self):
        library.sync(self.root, self.db, force=True)
        rid = library.find_by_file(self.db, "CNA", "c.html")["id"]
        for name in ("../../x.png", "..%2f..%2fx.png", "c.html"):
            r = self.client.get(f"/article-image?id={rid}&image={name}", headers={"Host": "localhost"})
            self.assertEqual(r.status_code, 404)

    def test_translate_article_validates_input(self):
        with self.client.session_transaction() as s:
            s["csrf"] = "tok"
        hdr = {"Host": "localhost", "X-CSRF-Token": "tok"}
        for body in ({"id": "1", "target": "ja"}, {"id": True, "target": "ja"}, {"id": 1, "target": "xx"}):
            self.assertEqual(self.client.post("/api/translate-article", json=body, headers=hdr).status_code, 400, body)
        self.assertEqual(self.client.post("/api/translate-article", json={"id": 999, "target": "ja"}, headers=hdr).status_code, 404)
        self.assertEqual(self.client.post("/api/translate-article", json={"id": 1, "target": "ja"}, headers={"Host": "localhost"}).status_code, 400)  # missing CSRF token

    def test_foreign_host_blocked(self):
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
