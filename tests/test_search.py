"""Search across languages: the English headline kept for every article (story_items.pivot)."""

import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_extractor import CNA, NORMAL

from newsreader.library import index
from newsreader.services import translation as ts
from newsreader.web import create_app

H = {"Host": "localhost"}


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        root = self.tmp / "articles"
        for site, name, data in [("BBC", "a.html", NORMAL), ("CNA", "c.html", CNA)]:
            (root / site).mkdir(parents=True, exist_ok=True)
            (root / site / name).write_bytes(data)
        self.db = self.tmp / "library.sqlite3"
        index._last_sync.clear()
        index.sync(root, self.db, force=True)
        self.cna = index.list_articles(self.db, source="CNA")["items"][0]["id"]
        with closing(sqlite3.connect(self.db)) as db:
            db.execute(
                "INSERT INTO story_items(article_id, title, lang, pivot) VALUES(?, ?, 'zh', ?)",
                (self.cna, "CNA headline", "Typhoon strikes Taiwan's east coast"),
            )
            db.commit()
        self.client = create_app(root, self.db, start_background=False, secret_key=b"s" * 32).test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ids(self, query, words):
        return [i["id"] for i in index.list_articles(self.db, query=query, pivot_words=words)["items"]]

    def test_words(self):
        self.assertEqual(
            index.search_words("The typhoon strikes Taiwan's coast, says CNA"),
            ["typhoon", "strike", "taiwan", "coast", "cna"],
        )
        self.assertEqual(index.search_words("台風"), [])
        self.assertEqual(len(index.search_words(" ".join(f"word{n}" for n in range(10)))), 6)

    def test_english_headline_finds_other_languages(self):
        self.assertEqual(self.ids("taiwan", index.search_words("taiwan")), [self.cna])
        self.assertEqual(self.ids("strike", ["strike"]), [self.cna])  # word start: strikes
        self.assertEqual(self.ids("aiwan", ["aiwan"]), [])  # not inside a word
        self.assertEqual(self.ids("typhoon taiwan", ["typhoon", "taiwan"]), [self.cna])
        self.assertEqual(self.ids("typhoon japan", ["typhoon", "japan"]), [])  # two words: both needed
        self.assertEqual(self.ids("typhoon taiwan japan", ["typhoon", "taiwan", "japan"]), [self.cna])  # 2 of 3
        self.assertEqual(
            self.ids("News Headline", None)[0:1], [i["id"] for i in index.list_articles(self.db, source="BBC")["items"]]
        )

    def test_japanese_search_is_translated_once_and_matches_the_english_headlines(self):
        calls = []

        def english(titles):
            calls.append(titles)
            return ["Taiwan typhoon"]

        with (
            mock.patch.object(ts, "provider_ready", lambda provider=None: True),
            mock.patch.object(ts, "translate_titles_to_english", english),
        ):
            html = self.client.get("/?q=台湾の台風", headers=H).get_data(as_text=True)
        self.assertIn("CNA headline", html)
        self.assertNotIn("News Headline", html)
        self.assertIn("英語に訳した「Taiwan typhoon」", html)
        self.assertEqual(calls, [["台湾の台風"]])

    def test_without_a_key_the_search_still_works(self):
        with (
            mock.patch.object(ts, "provider_ready", lambda provider=None: False),
            mock.patch.object(ts, "translate_titles_to_english", side_effect=AssertionError("must not translate")),
        ):
            page = self.client.get("/?q=台湾", headers=H)
        self.assertEqual(page.status_code, 200)
        self.assertIn("翻訳キーが必要", page.get_data(as_text=True))
        with (
            mock.patch.object(ts, "provider_ready", lambda provider=None: True),
            mock.patch.object(ts, "translate_titles_to_english", side_effect=ts.TranslationError("上限")),
        ):
            self.assertEqual(self.client.get("/?q=台湾", headers=H).status_code, 200)

    def test_english_search_needs_no_translation(self):
        with mock.patch.object(ts, "translate_titles_to_english", side_effect=AssertionError("must not translate")):
            html = self.client.get("/?q=typhoon", headers=H).get_data(as_text=True)
        self.assertIn("CNA headline", html)


if __name__ == "__main__":
    unittest.main()
