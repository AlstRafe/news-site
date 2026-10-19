"""Word card: meanings by part of speech from the Azure Translator dictionary (English words only)."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from newsreader.library import index
from newsreader.services import cache, settings
from newsreader.services import translation as ts
from newsreader.web import api, create_app
from test_extractor import NORMAL

H = {"Host": "localhost"}

FLY = [{"normalizedSource": "fly", "displaySource": "fly", "translations": [
    {"normalizedTarget": "ハエ", "displayTarget": "ハエ", "posTag": "NOUN", "confidence": 0.21, "prefixWord": "",
     "backTranslations": []},
    {"normalizedTarget": "飛ぶ", "displayTarget": "飛ぶ", "posTag": "VERB", "confidence": 0.41, "prefixWord": "",
     "backTranslations": [{"normalizedText": "fly", "displayText": "fly", "numExamples": 15, "frequencyCount": 4637}]},
    {"normalizedTarget": "飛行する", "displayTarget": "飛行する", "posTag": "VERB", "confidence": 0.12, "prefixWord": ""},
    {"normalizedTarget": "飛ぶ", "displayTarget": "飛ぶ", "posTag": "VERB", "confidence": 0.05, "prefixWord": ""},
    {"displayTarget": "", "posTag": "NOUN", "confidence": 0.5},
    {"displayTarget": "フライ", "posTag": "XYZ", "confidence": 0.01},
]}]


class DictionaryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.requests = []

        def fake_post(url, headers, data, provider):
            self.requests.append((url, headers, json.loads(data.decode("utf-8")), provider))
            return self.reply, {"x-metered-usage": "3"}

        self.reply = FLY
        self.patches = [mock.patch.object(ts, "_post_json", fake_post),
                        mock.patch.object(ts, "active_provider", lambda: "azure"),
                        mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3")]
        for patch in self.patches:
            patch.start()
        settings.set_secret("azure_api_key", "test-key-not-real")

    def tearDown(self):
        settings.delete_secret("azure_api_key")
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class LookupTests(DictionaryTestCase):
    def test_groups_by_part_of_speech_most_likely_first_and_caches(self):
        senses = ts.dictionary_lookup("Fly", "ja")
        self.assertEqual(senses, [{"pos": "動詞", "terms": ["飛ぶ", "飛行する"]},
                                  {"pos": "名詞", "terms": ["ハエ"]}, {"pos": "その他", "terms": ["フライ"]}])
        url, headers, body, provider = self.requests[0]
        self.assertTrue(url.startswith("https://api.cognitive.microsofttranslator.com/dictionary/lookup?"))
        self.assertIn("from=en", url)
        self.assertIn("to=ja", url)
        self.assertEqual(body, [{"Text": "Fly"}])
        self.assertEqual(headers["Ocp-Apim-Subscription-Key"], "test-key-not-real")
        self.assertEqual(provider, "azure")
        # the same word (any capitalisation) is not sent again; another language is
        self.assertEqual(ts.dictionary_lookup("fly", "ja"), senses)
        self.assertEqual(len(self.requests), 1)
        ts.dictionary_lookup("fly", "zh-hant")
        self.assertIn("to=zh-Hant", self.requests[1][0])
        usage = cache.monthly_translation_usage()["providers"]["azure"]
        self.assertEqual((usage["requests"], usage["source_chars"], usage["metered_chars"]), (2, 6, 6))

    def test_word_not_in_the_dictionary_is_remembered_as_empty(self):
        self.reply = [{"normalizedSource": "macron", "displaySource": "Macron", "translations": []}]
        self.assertEqual(ts.dictionary_lookup("Macron", "ja"), [])
        self.assertEqual(ts.dictionary_lookup("Macron", "ja"), [])
        self.assertEqual(len(self.requests), 1)

    def test_unexpected_reply_is_an_error_and_not_cached(self):
        self.reply = {"error": "x"}
        with self.assertRaises(ts.TranslationError):
            ts.dictionary_lookup("fly", "ja")
        self.reply = FLY
        self.assertTrue(ts.dictionary_lookup("fly", "ja"))

    def test_only_english_words_with_azure(self):
        for text, target, expected in [("fly", "ja", True), ("carry out", "ko", True), ("Prime Minister's", "ja", True),
                                       ("fly", "en", False), ("a b c d", "ja", False), ("안녕", "ja", False),
                                       ("飛ぶ", "ja", False), ("COVID-19", "ja", False), ("x" * 41, "ja", False),
                                       ("This is a sentence.", "ja", False)]:
            self.assertEqual(ts.dictionary_available(text, target), expected, text)


class ApiTests(DictionaryTestCase):
    def setUp(self):
        super().setUp()
        root = self.tmp / "articles"
        (root / "BBC").mkdir(parents=True)
        (root / "BBC" / "a.html").write_bytes(NORMAL)
        db = self.tmp / "library.sqlite3"
        index._last_sync.clear()
        index.sync(root, db, force=True)
        self.client = create_app(root, db, start_background=False, secret_key=b"d" * 32).test_client()
        self.client.get("/", headers=H)
        with self.client.session_transaction() as session:
            self.token = session["csrf"]

    def post(self, url, body, token=True):
        return self.client.post(url, headers={**H, **({"X-CSRF-Token": self.token} if token else {})}, json=body)

    def test_translation_says_whether_to_ask_the_dictionary(self):
        with mock.patch.object(ts, "_translate_batch", lambda provider, texts, target: [f"[{target}] {t}" for t in texts]), \
                mock.patch.object(api, "british_ipa", lambda text: None):
            self.assertTrue(self.post("/api/translate", {"text": "fly", "target": "ja"}).get_json()["dictionary"])
            self.assertFalse(self.post("/api/translate", {"text": "fly away now please", "target": "ja"}).get_json()["dictionary"])
        self.assertEqual(self.requests, [])  # the translation itself never calls the dictionary

    def test_dictionary_endpoint(self):
        reply = self.post("/api/dictionary", {"text": "fly", "target": "ja"})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.get_json()["senses"][0], {"pos": "動詞", "terms": ["飛ぶ", "飛行する"]})
        self.assertEqual(self.post("/api/dictionary", {"text": "長い文章です", "target": "ja"}).get_json(),
                         {"senses": [], "available": False})
        self.assertEqual(self.post("/api/dictionary", {"text": "fly", "target": "xx"}).status_code, 400)
        self.assertEqual(self.post("/api/dictionary", {"text": 3, "target": "ja"}).status_code, 400)
        self.assertEqual(self.post("/api/dictionary", {"text": "fly", "target": "ja"}, token=False).status_code, 400)
        self.assertEqual(len(self.requests), 1)

    def test_provider_error_is_a_safe_message(self):
        with mock.patch.object(ts, "_post_json", side_effect=ts.TranslationError("APIキーが認証されませんでした。")):
            reply = self.post("/api/dictionary", {"text": "fly", "target": "ja"})
        self.assertEqual(reply.status_code, 503)
        self.assertNotIn("test-key", reply.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
