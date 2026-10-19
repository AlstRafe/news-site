"""数字の見張り (D-59): recent multi-outlet groups compared in the background; the home page lists
the ones whose outlets disagree on a count."""
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.library import facts_watch, index
from newsreader.services import cache
from newsreader.services import translation as ts
from newsreader.web import create_app

H = {"Host": "localhost"}


def iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FactsWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "articles").mkdir()
        self.db = self.tmp / "library.sqlite3"
        self.contents = {}
        self.add_group("s1", [(1, "BBC", "en", "Missile strike on camp kills 50", "Missile strike on camp kills 50", 2,
                               "More than 50 people were killed in the strike."),
                              (2, "Yonhap", "ko", "미사일 공격…최소 20명 사망", "Missile attack... at least 20 dead", 5,
                               "공격으로 최소 20명이 숨졌다.")])
        self.add_group("s3", [(3, "BBC", "en", "Fire at factory, 3 dead", "Fire at factory, 3 dead", 3,
                               "Three people died in the fire."),
                              (4, "CNA", "zh", "工廠火災3死", "Factory fire kills 3", 4, "火災造成3人死亡。")])
        self.translated = []

        def to_english(texts):
            self.translated.append(list(texts))
            table = {"공격으로 최소 20명이 숨졌다.": "At least 20 people were killed in the attack.",
                     "火災造成3人死亡。": "The fire killed 3 people."}
            return [table.get(t, t) for t in texts]

        self.to_english = to_english
        self.patches = [mock.patch.object(index, "get_content", lambda db, aid: self.contents.get(aid)),
                        mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_group(self, key, rows):
        with closing(index.connect(self.db)) as db:
            for aid, source, lang, title, pivot, hours, body in rows:
                db.execute("INSERT INTO articles(id, source, filename, title, published, paragraph_count, size, mtime)"
                           " VALUES(?,?,?,?,?,9,1,1)", (aid, source, f"{aid}.html", title, iso(hours)))
                db.execute("INSERT INTO story_items(article_id, title, lang, pivot, pivot_chars, pivot_at) VALUES(?,?,?,?,0,'')",
                           (aid, title, lang, pivot))
                db.execute("INSERT INTO story_members(article_id, story_key, score) VALUES(?,?,0.5)", (aid, key))
                self.contents[aid] = {"paragraphs": [body]}

    def test_disagreements_are_found_once_and_listed(self):
        result = facts_watch.refresh(self.db, self.to_english)
        self.assertEqual((result["compared"], result["differs"]), (2, 1))
        listed = facts_watch.differing(self.db)
        self.assertEqual([d["key"] for d in listed], ["s1"])
        trail = listed[0]["kinds"][0]["trail"]
        self.assertEqual([(t["source"], t["text"]) for t in trail], [("Yonhap", "少なくとも 20人"), ("BBC", "50人超")])
        calls = len(self.translated)
        self.assertEqual(facts_watch.refresh(self.db, self.to_english)["compared"], 0)  # nothing new
        self.assertEqual(len(self.translated), calls)

    def test_nothing_is_translated(self):
        result = facts_watch.refresh(self.db)  # Korean and Chinese counts are read as written
        self.assertEqual((result["compared"], result["differs"]), (2, 1))
        self.assertEqual(self.translated, [])

    def test_a_regrouped_story_is_not_listed_with_an_old_summary(self):
        facts_watch.refresh(self.db, self.to_english)
        self.add_group("s1", [(5, "CNA", "zh", "飛彈攻擊", "Missile attack", 1, "飛彈攻擊。")])
        self.assertEqual(facts_watch.differing(self.db), [])
        facts_watch.refresh(self.db, self.to_english)  # members changed: compared again
        self.assertEqual([d["key"] for d in facts_watch.differing(self.db)], ["s1"])

    def test_a_loosely_chained_group_is_not_put_on_the_home_page(self):
        with closing(index.connect(self.db)) as db:
            db.execute("UPDATE story_members SET score = 0.1 WHERE article_id = 2")
        facts_watch.refresh(self.db, self.to_english)
        self.assertEqual(facts_watch.differing(self.db), [])

    def test_old_groups_are_left_alone(self):
        later = datetime.now(timezone.utc) + timedelta(days=5)
        self.assertEqual(facts_watch.refresh(self.db, self.to_english, now=later)["compared"], 0)

    def test_home_page_lists_the_disagreement(self):
        facts_watch.refresh(self.db, self.to_english)
        with mock.patch.object(ts, "_translate_batch", lambda provider, texts, target: texts):
            page = create_app(self.tmp / "articles", self.db, start_background=False,
                              secret_key=b"f" * 32).test_client().get("/", headers=H).get_data(as_text=True)
        self.assertIn("数字が食い違うニュース", page)
        self.assertIn('href="/stories/s1#facts"', page)
        self.assertIn("少なくとも 20人", page)
        self.assertNotIn("/stories/s3#facts", page)  # the outlets agree on 3

    def test_group_list_and_page_flag_the_disagreement(self):
        facts_watch.refresh(self.db, self.to_english)
        self.assertEqual(list(facts_watch.disputed(self.db, ["s1", "s3"])), ["s1"])
        client = create_app(self.tmp / "articles", self.db, start_background=False, secret_key=b"f" * 32).test_client()
        with mock.patch.object(ts, "_translate_batch", lambda provider, texts, target: texts):
            listing = client.get("/stories", headers=H).get_data(as_text=True)
            page = client.get("/stories/s1", headers=H).get_data(as_text=True)
        self.assertIn("死者の数字が違う", listing)
        with mock.patch.object(ts, "_translate_batch", lambda provider, texts, target: texts):
            only = client.get("/stories?scope=disputes", headers=H).get_data(as_text=True)
        self.assertIn("/stories/s1", only)
        self.assertNotIn("/stories/s3", only)
        self.assertIn("死者の数字が媒体で違います", page)

    def test_article_page_says_its_group_disagrees(self):
        facts_watch.refresh(self.db)
        client = create_app(self.tmp / "articles", self.db, start_background=False, secret_key=b"f" * 32).test_client()
        with mock.patch("newsreader.web.pages.load_article", lambda record: {"title": "t", "paragraphs": ["p"], "kind": "k",
                                                                            "paragraph_count": 1, "images": [], "url": ""}), \
                mock.patch.object(ts, "_translate_batch", lambda provider, texts, target: texts):
            page = client.get("/a/1", headers=H).get_data(as_text=True)
            other = client.get("/a/3", headers=H).get_data(as_text=True)
        self.assertIn('class="article-dispute" href="/stories/s1#facts"', page)
        self.assertNotIn("article-dispute", other)

    def test_summary_keeps_compared_counts_only(self):
        facts_watch.refresh(self.db, self.to_english)
        with closing(index.connect(self.db)) as db:
            kinds = [k["kind"] for k in json.loads(db.execute("SELECT summary FROM story_facts WHERE story_key='s1'").fetchone()[0])]
        self.assertEqual(kinds, ["dead"])


if __name__ == "__main__":
    unittest.main()
