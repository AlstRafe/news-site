"""保存（後で読む）, 既読 and 単語帳: kept beside the index, named by outlet and file."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_extractor import CNA, NAVER, NORMAL

from newsreader.library import index, reading
from newsreader.services import cache
from newsreader.services import translation as ts
from newsreader.web import create_app

H = {"Host": "localhost"}


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        root = self.tmp / "articles"
        for site, name, data in [("BBC", "a.html", NORMAL), ("NAVER NEWS", "n.html", NAVER), ("CNA", "c.html", CNA)]:
            (root / site).mkdir(parents=True, exist_ok=True)
            (root / site / name).write_bytes(data)
        self.db = self.tmp / "library.sqlite3"
        index._last_sync.clear()
        index.sync(root, self.db, force=True)
        self.patches = [
            mock.patch.object(ts, "_translate_batch", lambda p, texts, t: [f"[{t}] {x}" for x in texts]),
            mock.patch.object(ts, "active_provider", lambda: "deepl"),
            mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3"),
        ]
        for patch in self.patches:
            patch.start()
        self.app = create_app(root, self.db, start_background=False, secret_key=b"r" * 32)
        self.client = self.app.test_client()
        self.client.get("/", headers=H)
        with self.client.session_transaction() as session:
            self.hdr = {**H, "X-CSRF-Token": session["csrf"]}
        self.ids = [item["id"] for item in index.list_articles(self.db)["items"]]
        self.marks = reading.path_for(self.db)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_opening_an_article_marks_it_read_in_the_list(self):
        self.assertNotIn("is-read", self.client.get("/", headers=H).get_data(as_text=True))
        self.client.get(f"/a/{self.ids[0]}", headers=H)
        page = self.client.get("/", headers=H).get_data(as_text=True)
        self.assertIn(f'class="card is-read" data-id="{self.ids[0]}"', page)

    def test_saved_articles_survive_a_rebuilt_index(self):
        r = self.client.post("/api/saved", headers=self.hdr, json={"id": self.ids[1], "saved": True})
        self.assertEqual(r.get_json(), {"saved": True})
        self.assertIn(
            'aria-pressed="true">保存済み', self.client.get(f"/a/{self.ids[1]}", headers=H).get_data(as_text=True)
        )
        title = index.get(self.db, self.ids[1])["title"]
        # the index is rebuilt from the files: ids change, the saved article is still found
        self.db.unlink()
        index._last_sync.clear()
        index.sync(self.tmp / "articles", self.db, force=True)
        page = self.client.get("/saved", headers=H).get_data(as_text=True)
        self.assertIn(title, page)
        new_id = index.find_by_file(self.db, *reading.saved(self.marks)[0]["key"].split("/", 1))["id"]
        self.assertIn(f'href="/a/{new_id}"', page)
        self.client.post("/api/saved", headers=self.hdr, json={"id": new_id, "saved": False})
        self.assertIn("まだ保存した記事はありません", self.client.get("/saved", headers=H).get_data(as_text=True))

    def test_bad_requests(self):
        for body in ({"id": "1", "saved": True}, {"id": True, "saved": True}, {}):
            self.assertEqual(self.client.post("/api/saved", headers=self.hdr, json=body).status_code, 400)
        self.assertEqual(
            self.client.post("/api/saved", headers=self.hdr, json={"id": 999999, "saved": True}).status_code, 404
        )
        self.assertEqual(
            self.client.post("/api/saved", headers=H, json={"id": self.ids[0], "saved": True}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/words", headers=self.hdr, json={"word": "", "target": "ja"}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/words", headers=self.hdr, json={"word": "x", "target": "xx"}).status_code, 400
        )
        # a target that is no string at all is refused too (it was a server error)
        self.assertEqual(
            self.client.post("/api/words", headers=self.hdr, json={"word": "x", "target": ["ja"]}).status_code, 400
        )

    def test_word_book(self):
        body = {
            "word": "ceasefire",
            "target": "ja",
            "meaning": "停戦",
            "sentence": "A ceasefire was agreed.",
            "id": self.ids[0],
        }
        self.assertEqual(self.client.post("/api/words", headers=self.hdr, json=body).status_code, 200)
        self.client.post(
            "/api/words", headers=self.hdr, json={**body, "meaning": "休戦"}
        )  # again: updated, not doubled
        entries = reading.words(self.marks)
        self.assertEqual([(e["word"], e["meaning"]) for e in entries], [("ceasefire", "休戦")])
        page = self.client.get("/words", headers=H).get_data(as_text=True)
        self.assertIn("A ceasefire was agreed.", page)
        self.assertIn(f'href="/a/{self.ids[0]}"', page)
        self.client.post("/api/words/delete", headers=self.hdr, json={"word_id": entries[0]["id"]})
        self.assertEqual(reading.words(self.marks), [])


if __name__ == "__main__":
    unittest.main()
