import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.web import app as app_module
from newsreader.collect import runner as collector
from newsreader.library import index as library
from newsreader.services import cache as local_cache
from newsreader.services import translation as ts
from test_extractor import CNA, NORMAL


class FakeProviderMixin:
    """Replace the network call and isolate the cache so real data is never touched."""

    def start_fake_provider(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.calls = []
        self._saved = (local_cache.CACHE_PATH, ts.active_provider, ts._translate_batch)
        local_cache.CACHE_PATH = self.tmp / "cache.sqlite3"
        ts.active_provider = lambda: "deepl"

        def fake(provider, texts, target):
            self.calls.append(list(texts))
            return [f"T:{text}" for text in texts]

        ts._translate_batch = fake

    def stop_fake_provider(self):
        local_cache.CACHE_PATH, ts.active_provider, ts._translate_batch = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)


class ParagraphTranslationTests(FakeProviderMixin, unittest.TestCase):
    def setUp(self):
        self.start_fake_provider()

    def tearDown(self):
        self.stop_fake_provider()

    def test_output_aligns_with_input_and_blanks_stay_blank(self):
        out = ts.translate_paragraphs(["Title", "P1", "", "P2"], "ja", limit=1000)
        self.assertEqual(out, ["T:Title", "T:P1", "", "T:P2"])
        self.assertEqual(len(self.calls), 1)  # one provider request for the whole article

    def test_overlong_paragraph_is_split_and_rejoined_to_one_item(self):
        long_paragraph = " ".join(["word"] * 2500)  # ~12,500 chars
        out = ts.translate_paragraphs(["Head", long_paragraph, "Tail"], "ko", limit=50_000)
        self.assertEqual(len(out), 3)
        self.assertGreaterEqual(out[1].count("T:"), 3)
        self.assertEqual(out[0], "T:Head")
        self.assertEqual(out[2], "T:Tail")

    def test_second_call_uses_cache(self):
        ts.translate_paragraphs(["A", "B"], "ja", limit=100)
        before = len(self.calls)
        again = ts.translate_paragraphs(["A", "B"], "ja", limit=100)
        self.assertEqual(again, ["T:A", "T:B"])
        self.assertEqual(len(self.calls), before)

    def test_existing_translate_text_behaviour_is_unchanged(self):
        self.assertEqual(ts.translate_text("a\n\nb", "ja", limit=100), "T:a\n\nT:b")

    def test_rejections(self):
        with self.assertRaises(ts.TranslationError):
            ts.translate_paragraphs(["x"], "xx", limit=100)
        with self.assertRaises(ts.TranslationError):
            ts.translate_paragraphs(["", "  "], "ja", limit=100)
        with self.assertRaises(ts.TranslationError):
            ts.translate_paragraphs(["x" * 50], "ja", limit=10)
        ts.active_provider = lambda: "azure"
        with self.assertRaises(ts.TranslationError):
            ts.translate_paragraphs(["x"], "en-gb", limit=100)


class BilingualEndpointTests(FakeProviderMixin, unittest.TestCase):
    def setUp(self):
        self.start_fake_provider()
        self.root = self.tmp / "articles"
        (self.root / "CNA").mkdir(parents=True)
        (self.root / "CNA" / "c.html").write_bytes(CNA)
        self.db = self.tmp / "library.sqlite3"
        app_module.DEFAULT_DIRECTORY, app_module.LIBRARY_DB = self.root, self.db
        library._last_sync.clear()
        library.sync(self.root, self.db, force=True)
        self.rid = library.find_by_file(self.db, "CNA", "c.html")["id"]
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as s:
            s["csrf"] = "tok"
        self.hdr = {"Host": "localhost", "X-CSRF-Token": "tok"}

    def tearDown(self):
        self.stop_fake_provider()

    def test_returns_one_translation_per_paragraph_plus_title(self):
        r = self.client.post("/api/translate-paragraphs", json={"id": self.rid, "target": "ja"}, headers=self.hdr)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        article = app_module.extract_article((self.root / "CNA" / "c.html").read_bytes())
        self.assertEqual(len(body["paragraphs"]), len(article["paragraphs"]))
        self.assertEqual(body["title"], "T:" + article["title"])
        self.assertEqual(body["paragraphs"][0], "T:" + article["paragraphs"][0])

    def test_validation_and_csrf(self):
        post = lambda body, h=self.hdr: self.client.post("/api/translate-paragraphs", json=body, headers=h).status_code
        self.assertEqual(post({"id": "1", "target": "ja"}), 400)
        self.assertEqual(post({"id": True, "target": "ja"}), 400)
        self.assertEqual(post({"id": self.rid, "target": "xx"}), 400)
        self.assertEqual(post({"id": 9999, "target": "ja"}), 404)
        self.assertEqual(post({"id": self.rid, "target": "ja"}, {"Host": "localhost"}), 400)

    def test_article_page_has_bilingual_button(self):
        html = self.client.get(f"/a/{self.rid}", headers={"Host": "localhost"}).get_data(as_text=True)
        self.assertIn('id="translate-bilingual"', html)
        self.assertIn('id="bi-title"', html)


class CollectorIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "articles"
        (self.root / "BBC").mkdir(parents=True)
        (self.root / "BBC" / "a.html").write_bytes(NORMAL)
        self._saved = library.DEFAULT_DB_PATH
        library.DEFAULT_DB_PATH = self.tmp / "library.sqlite3"

    def tearDown(self):
        library.DEFAULT_DB_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_articles_are_indexed_immediately_even_inside_throttle_window(self):
        first = collector.update_library_index(self.root)
        self.assertEqual(first["added"], 1)
        (self.root / "BBC" / "b.html").write_bytes(CNA)  # saved by the next collection cycle
        second = collector.update_library_index(self.root)  # runs again at once: must not be throttled
        self.assertEqual(second["added"], 1)
        self.assertEqual(library.list_articles(library.DEFAULT_DB_PATH)["total"], 2)

    def test_index_failure_does_not_fail_collection(self):
        self.assertIsNone(collector.update_library_index(self.tmp / "missing"))


if __name__ == "__main__":
    unittest.main()
