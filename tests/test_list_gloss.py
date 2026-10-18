"""List page: "見出しを訳す" — headline translations, cached one by one, never more than one page."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from newsreader.library import index
from newsreader.services import cache
from newsreader.services import translation as ts
from newsreader.web import create_app
from test_extractor import CNA, NAVER, NORMAL

H = {"Host": "localhost"}


class ListGlossTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        root = self.tmp / "articles"
        for site, name, data in [("BBC", "a.html", NORMAL), ("NAVER NEWS", "n.html", NAVER), ("CNA", "c.html", CNA)]:
            (root / site).mkdir(parents=True, exist_ok=True)
            (root / site / name).write_bytes(data)
        self.db = self.tmp / "library.sqlite3"
        index._last_sync.clear()
        index.sync(root, self.db, force=True)
        self.app = create_app(root, self.db, start_background=False, secret_key=b"g" * 32)
        self.client = self.app.test_client()
        self.calls = []

        def fake(provider, texts, target):
            self.calls.append(list(texts))
            return [f"[{target}] {t}" for t in texts]

        self.patches = [mock.patch.object(ts, "_translate_batch", fake),
                        mock.patch.object(ts, "active_provider", lambda: "deepl"),
                        mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3")]
        for patch in self.patches:
            patch.start()
        self.client.get("/", headers=H)
        with self.client.session_transaction() as session:
            self.token = session["csrf"]
        self.ids = [item["id"] for item in index.list_articles(self.db)["items"]]

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, body):
        return self.client.post("/api/translate-titles", headers={**H, "X-CSRF-Token": self.token}, json=body)

    def test_translates_each_headline_once(self):
        response = self.post({"ids": self.ids, "target": "ja"})
        self.assertEqual(response.status_code, 200)
        translations = response.get_json()["translations"]
        self.assertEqual(len(translations), 3)
        self.assertTrue(all(text.startswith("[ja] ") for text in translations.values()))
        self.assertEqual(sum(len(c) for c in self.calls), 3)
        # Another page mixing a known headline: only nothing new is sent.
        self.post({"ids": self.ids[:2], "target": "ja"})
        self.assertEqual(sum(len(c) for c in self.calls), 3)
        self.post({"ids": self.ids[:1], "target": "ko"})  # another language is another translation
        self.assertEqual(sum(len(c) for c in self.calls), 4)

    def test_rejects_bad_requests(self):
        for body in ({"ids": [], "target": "ja"}, {"ids": list(range(1, 50)), "target": "ja"},
                     {"ids": ["1"], "target": "ja"}, {"ids": [1], "target": "xx"}, {"ids": [True], "target": "ja"}):
            self.assertEqual(self.post(body).status_code, 400, body)
        self.assertEqual(self.post({"ids": [999999], "target": "ja"}).get_json(), {"translations": {}})
        self.assertEqual(self.client.post("/api/translate-titles", headers=H, json={"ids": self.ids, "target": "ja"}).status_code, 400)

    def test_provider_error_is_a_message(self):
        with mock.patch.object(ts, "_translate_batch", side_effect=ts.TranslationError("上限に達しました")):
            response = self.post({"ids": self.ids, "target": "ja"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("上限", response.get_json()["error"])

    def test_list_page_offers_the_toggle(self):
        html = self.client.get("/", headers=H).get_data(as_text=True)
        self.assertIn('id="gloss-toggle"', html)
        self.assertIn(f'data-id="{self.ids[0]}"', html)


if __name__ == "__main__":
    unittest.main()
