"""読み比べ「違いを見る」(D-56): figures and words compared across the outlets of one news group."""
import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.library import compare, index
from newsreader.services import cache, sentences
from newsreader.services import translation as ts
from newsreader.web import create_app

H = {"Host": "localhost"}


def kinds(text):
    return [(f.kind, f.text) for f in compare.facts_in(text)]


class FactExtractionTests(unittest.TestCase):
    def test_counts_of_people_by_kind(self):
        self.assertEqual(kinds("Houthi missile strike on Yemen camp kills 50"), [("dead", "50人")])
        self.assertEqual(kinds("A missile hit the base, killing at least 20 people."), [("dead", "少なくとも 20人")])
        self.assertEqual(kinds("The death toll rose to 53 on Sunday."), [("dead", "53人")])
        self.assertEqual(kinds("He killed 5 people and injured 20."), [("dead", "5人"), ("injured", "20人")])
        self.assertEqual(kinds("Two people were killed and 15 injured, while dozens remain missing."),
                         [("injured", "15人"), ("dead", "2人"), ("missing", "数十人")])
        self.assertEqual(kinds("More than 1,200 protesters were detained in Paris."), [("detained", "1,200人超")])
        self.assertEqual(kinds("The attack killed more than 50 people and injuring about 200, officials said."),
                         [("dead", "50人超"), ("injured", "約 200人")])

    def test_money_percent_age_and_what_is_not_a_figure(self):
        self.assertEqual(kinds("Iraq lost about $1.4bn."), [("money", "約 14億ドル")])
        self.assertEqual(kinds("It earned 5.2 billion won."), [("money", "52億ウォン")])
        self.assertEqual(kinds("Support fell to 47%, the lowest since 2021."), [("percent", "47%")])
        self.assertEqual(kinds("The gap narrowed to 0.6 percentage points."), [("percent", "0.6ポイント")])
        self.assertEqual(kinds("A 54-year-old man was arrested."), [("age", "54歳")])
        for text in ("The G7 met for the 19th time.", "It happened in 2026 at 10:30.", "on 19 October",
                     "Musk will give every child one share.", "The road is 5m wide."):
            self.assertEqual(kinds(text), [], text)

    def test_outlets_that_disagree_on_a_count_are_flagged(self):
        members = [
            {"id": 1, "source": "BBC", "published": "2026-10-19T16:20:00Z", "english": "Missile strike kills 50 at Yemen camp."},
            {"id": 2, "source": "Yonhap", "published": "2026-10-19T11:06:00Z", "english": "Missile attack: at least 20 dead."},
            {"id": 3, "source": "Yonhap", "published": "2026-10-19T12:00:00Z", "english": "The toll rose to 53 dead. 12% of the camp burned."},
            {"id": 4, "source": "CNA", "published": "2026-10-19T13:00:00Z", "english": "Rescuers found 50 bodies; 30% of the camp burned."},
        ]
        found = {c["kind"]: c for c in compare.compare(members)}
        self.assertTrue(found["dead"]["differs"])
        self.assertEqual([r["id"] for r in found["dead"]["rows"]], [2, 3, 4, 1])  # oldest report first
        self.assertFalse(found["percent"]["compared"])  # shown side by side, never called a discrepancy
        self.assertFalse(found["percent"]["differs"])
        same = compare.compare([dict(m, english="Strike kills 50.") for m in members[:2]])
        self.assertFalse(same[0]["differs"])

    def test_a_kind_one_outlet_reports_is_left_out(self):
        members = [{"id": 1, "source": "BBC", "published": "", "english": "50 dead."},
                   {"id": 2, "source": "BBC", "published": "", "english": "52 dead."},
                   {"id": 3, "source": "CNA", "published": "", "english": "A fire broke out."}]
        self.assertEqual(compare.compare(members), [])


class FramingTests(unittest.TestCase):
    def test_words_only_one_outlet_uses_and_shared_ones(self):
        members = [
            {"source": "BBC", "lang": "en", "pivot": "Houthi missile strike on Yemen camp kills 50"},
            {"source": "Yonhap", "lang": "ko", "pivot": "Houthis launch ballistic missile at Yemeni government forces base"},
            {"source": "Yonhap", "lang": "ko", "pivot": "Houthi missile kills at least 53 in Yemen"},
        ]
        found = compare.framing(members)
        self.assertIn("missile", found["shared"])
        self.assertIn("houthi", [w.lower() for w in found["shared"]])
        self.assertIn("camp", found["only"]["BBC"])
        self.assertIn("ballistic", found["only"]["Yonhap"])
        self.assertNotIn("missile", found["only"]["Yonhap"])

    def test_one_outlet_has_nothing_to_compare(self):
        self.assertEqual(compare.framing([{"source": "BBC", "pivot": "A b c"}]), {"shared": [], "only": {}})


class CompareApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "articles").mkdir()
        self.db = self.tmp / "library.sqlite3"
        rows = [(1, "BBC", "Houthi missile strike on Yemen camp kills 50", "en", "Houthi missile strike on Yemen camp kills 50",
                 "2026-10-19T16:20:00Z"),
                (2, "Yonhap", "후티, 예멘 정부군 기지에 탄도미사일…최소 53명 사망", "ko",
                 "Houthis fire ballistic missile at Yemeni government base... at least 53 dead", "2026-10-19T12:00:00Z")]
        with closing(index.connect(self.db)) as db:
            for aid, source, title, lang, pivot, published in rows:
                db.execute("INSERT INTO articles(id, source, filename, title, published, paragraph_count, size, mtime)"
                           " VALUES(?,?,?,?,?,9,1,1)", (aid, source, f"{aid}.html", title, published))
                db.execute("INSERT INTO story_items(article_id, title, lang, pivot, pivot_chars, pivot_at) VALUES(?,?,?,?,0,'')",
                           (aid, title, lang, pivot))
                db.execute("INSERT INTO story_members(article_id, story_key, score) VALUES(?, 's1', 0.5)", (aid,))
        contents = {1: {"paragraphs": ["The Houthis fired a missile at a camp in Yemen, killing 50 people.", "More later."]},
                    2: {"paragraphs": ["후티 반군이 미사일을 발사했다. 최소 53명이 숨졌다고 정부가 밝혔다."]}}
        self.sent = []

        def fake_batch(provider, texts, target):
            self.sent.append((target, list(texts)))
            if target == "en":
                out = ["The Houthi rebels fired a missile. At least 53 people were killed, the government said."
                       if t.startswith("후티") else t for t in texts]
            else:
                out = [f"[{target}] {t}" for t in texts]
            for source, translated in zip(texts, out):
                if source.startswith("후티 반군"):
                    sentences.remember(target, source, translated, [18, 21], [35, 52])
            return out

        self.patches = [mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3"),
                        mock.patch.object(ts, "active_provider", lambda: "azure"),
                        mock.patch.object(ts, "_translate_batch", fake_batch),
                        mock.patch.object(index, "get_content", lambda db, aid: contents.get(aid))]
        for patch in self.patches:
            patch.start()
        self.client = create_app(self.tmp / "articles", self.db, start_background=False, secret_key=b"c" * 32).test_client()
        self.client.get("/", headers=H)
        with self.client.session_transaction() as session:
            self.csrf = session["csrf"]

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, path, body):
        return self.client.post(path, json=body, headers={**H, "X-CSRF-Token": self.csrf})

    def test_figures_are_compared_with_each_outlets_own_sentence(self):
        reply = self.post("/api/story-facts", {"key": "s1", "target": "ja"})
        self.assertEqual(reply.status_code, 200)
        dead = {c["kind"]: c for c in reply.get_json()["categories"]}["dead"]
        self.assertTrue(dead["differs"])
        yonhap = next(r for r in dead["rows"] if r["source"] == "Yonhap")
        own = [f["original"] for f in yonhap["facts"]]
        self.assertIn("최소 53명이 숨졌다고 정부가 밝혔다.", own)  # the outlet's own sentence, found via sentence lengths
        shown = [f["shown"] for f in yonhap["facts"]]
        self.assertIn("[ja] 최소 53명이 숨졌다고 정부가 밝혔다.", shown)  # translated from the original, not from English
        english_sent = [t for target, texts in self.sent if target == "en" for t in texts]
        self.assertFalse(any("Houthis fired a missile at a camp" in t for t in english_sent))  # BBC is not translated

    def test_words_glossed_in_the_readers_language(self):
        reply = self.post("/api/story-words", {"key": "s1", "target": "ja"}).get_json()
        only = {entry["source"]: [w["word"] for w in entry["words"]] for entry in reply["only"]}
        self.assertIn("camp", only["BBC"])
        self.assertIn("ballistic", only["Yonhap"])
        self.assertTrue(all(w["gloss"].startswith("[ja]") for w in reply["shared"]))

    def test_bad_requests(self):
        self.assertEqual(self.post("/api/story-facts", {"key": "s999", "target": "ja"}).status_code, 404)
        self.assertEqual(self.post("/api/story-facts", {"key": "x'1", "target": "ja"}).status_code, 400)
        self.assertEqual(self.post("/api/story-words", {"key": "s1", "target": "fr"}).status_code, 400)
        self.assertEqual(self.client.post("/api/story-words", json={"key": "s1", "target": "ja"}, headers=H).status_code, 400)

    def test_group_page_offers_the_comparisons(self):
        page = self.client.get("/stories/s1", headers=H).get_data(as_text=True)
        self.assertIn('id="compare-facts"', page)
        self.assertIn('id="compare-words"', page)


if __name__ == "__main__":
    unittest.main()
