"""Sentence-by-sentence pairing of an original and its translation (D-57)."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.services import cache, relay, sentences, settings
from newsreader.services import translation as ts

REAL_BATCH = ts._translate_batch  # taken at import, before the provider guard (conftest) replaces it


class SentenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3"),
                        mock.patch.object(settings, "SETTINGS_PATH", self.tmp / "settings.json")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        settings.delete_secret("azure_api_key")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_split_keeps_every_character(self):
        for text in ("One. Two! Three?", "가나다. 라마바.", "一句。二句！三句", "No end", "A.  B. "):
            self.assertEqual("".join(sentences.split(text)), text, text)
        self.assertEqual(len(sentences.split("一句。二句！三句")), 3)
        self.assertEqual(len(sentences.split("He said \"Stop.\" Then left.")), 2)

    def test_azure_lengths_are_kept_and_used(self):
        settings.set_secret("azure_api_key", "k")
        source, translated = "사고가 났다. 3명이 다쳤다.", "An accident happened. Three were hurt."
        payload = [{"translations": [{"text": translated, "to": "en",
                                      "sentLen": {"srcSentLen": [8, 8], "transSentLen": [22, 16]}}]}]
        with mock.patch.object(ts, "_post_json", return_value=(payload, {})) as post:
            self.assertEqual(REAL_BATCH("azure", [source], "en"), [translated])
        self.assertIn("includeSentenceLength=true", post.call_args.args[0])
        self.assertEqual(sentences.spans("en", source, translated), {"src": [8, 8], "tr": [22, 16]})
        self.assertEqual(sentences.pairs("en", source, translated),
                         [("사고가 났다.", "An accident happened."), ("3명이 다쳤다.", "Three were hurt.")])

    def test_lengths_that_do_not_add_up_fall_back_to_punctuation(self):
        sentences.remember("ja", "A. B.", "あ。い。", [1, 1], [1, 1])  # wrong sums
        self.assertEqual(sentences.pairs("ja", "A. B.", "あ。い。"), [("A.", "あ。"), ("B.", "い。")])
        self.assertIsNone(sentences.spans("ja", "A. B. C.", "あ。い。"))  # counts differ: no pairing
        self.assertIsNone(sentences.spans("ja", "Only one", "ひとつ"))

    def test_lengths_are_in_utf16_units_for_the_browser(self):
        source, translated = "😀 Hi. Bye.", "😀 やあ。またね。"
        found = sentences.spans("ja", source, translated)
        self.assertEqual(sum(found["src"]), len(source.encode("utf-16-le")) // 2)
        self.assertEqual(sum(found["tr"]), len(translated.encode("utf-16-le")) // 2)

    def test_relay_passes_lengths_on(self):
        out = {"translations": ["X. Y."], "sentences": [{"src": [2, 3], "tr": [3, 2]}]}
        with mock.patch.object(relay, "_post", return_value=out):
            self.assertEqual(relay._translate_batch(["A. BC"], "en"), ["X. Y."])
        self.assertEqual(sentences.reported("en", "A. BC", "X. Y."), {"src": [2, 3], "tr": [3, 2]})



class ParagraphPairsApiTests(unittest.TestCase):
    """/api/translate-paragraphs tells the page where each sentence starts on both sides."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_extractor import NORMAL
        from newsreader.library import index as library
        from newsreader.web import create_app
        self.tmp = Path(tempfile.mkdtemp())
        root = self.tmp / "articles" / "BBC"
        root.mkdir(parents=True)
        (root / "a.html").write_bytes(NORMAL.replace(b"News paragraph one.", b"First sentence here. Then a second one."))
        library._last_sync.clear()
        self.patches = [mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3"),
                        mock.patch.object(ts, "active_provider", lambda: "azure"),
                        mock.patch.object(ts, "_translate_batch",
                                          lambda provider, texts, target: [t.replace(".", "。") for t in texts])]
        for patch in self.patches:
            patch.start()
        db = self.tmp / "library.sqlite3"
        library.sync(self.tmp / "articles", db, force=True)
        self.client = create_app(self.tmp / "articles", db, start_background=False, secret_key=b"s" * 32).test_client()
        self.client.get("/", headers={"Host": "localhost"})
        with self.client.session_transaction() as session:
            self.csrf = session["csrf"]
        self.article_id = library.list_articles(db)["items"][0]["id"]

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sentence_lengths_cover_both_texts(self):
        reply = self.client.post("/api/translate-paragraphs", json={"id": self.article_id, "target": "ja"},
                                 headers={"Host": "localhost", "X-CSRF-Token": self.csrf}).get_json()
        self.assertEqual(len(reply["sentences"]), len(reply["paragraphs"]))
        paired = [(p, s) for p, s in zip(reply["paragraphs"], reply["sentences"]) if s]
        self.assertTrue(paired, "no paragraph with two or more sentences was paired")
        for translated, spans in paired:
            self.assertEqual(sum(spans["tr"]), len(translated.encode("utf-16-le")) // 2)
            self.assertEqual(len(spans["src"]), len(spans["tr"]))


if __name__ == "__main__":
    unittest.main()
