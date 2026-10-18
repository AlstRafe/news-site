"""Phase 7: same news across outlets — pivots, budget, grouping, storage, pages, settings.

Translation is always a fake here (conftest also blocks the real providers). The label-based
evaluation on the 130 real articles runs only when NEWS_REALDATA points at an ``articles``
folder; its thresholds are regression floors set from the first measured run (D-21), not
quality claims.
"""
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from newsreader.library import index, pipeline, status, stories
from newsreader.services import settings
from newsreader.services.translation import TranslationError
from newsreader.web import create_app

H = {"Host": "localhost"}
LABELS = Path(__file__).resolve().parent / "fixtures" / "story_labels.json"


def bbc(title: str, published: str) -> bytes:
    return f'''<html><head><meta name="pagetype" content="News Article">
<meta property="article:published_time" content="{published}"></head><body>
<main id="bbc-main"><h1>{title}</h1><article>
<div data-component="layout-block"><p class="Paragraph-styles__ParagraphStyled-x">Body of {title}.</p></div>
</article></main></body></html>'''.encode()


def cna(title: str, published: str) -> bytes:
    return f'''<html><head><link rel="canonical" href="https://www.cna.com.tw/news/aopl/x.aspx">
<meta property="article:published_time" content="{published}"></head><body>
<article class="article"><h1>{title}</h1><div class="centralContent">
<div class="paragraph"><p>本文。</p></div></div></article></body></html>'''.encode()


NOBEL_A = "Two US scholars win Nobel economics prize for business cycle models"
NOBEL_B = "Nobel economics prize goes to two US scholars for business cycle models"
NOBEL_ZH = "美2學者獲諾貝爾經濟學獎　商業週期模型"
NOBEL_ZH_EN = "Two US scholars win Nobel economics prize for business cycle models"
OTHER = "Heavy snow closes mountain passes across the Alps"


class FakeTranslator:
    def __init__(self, table=None, error=None):
        self.table, self.error, self.calls = table or {}, error, []

    def __call__(self, titles):
        self.calls.append(list(titles))
        if self.error:
            raise self.error
        return [self.table.get(t, "") for t in titles]


class StoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "articles"
        for site in ("BBC", "CNA"):
            (self.root / site).mkdir(parents=True)
        self.write("BBC", "a.html", bbc(NOBEL_A, "2026-10-13T10:00:00Z"))
        self.write("BBC", "b.html", bbc(NOBEL_B, "2026-10-13T12:00:00Z"))
        self.write("BBC", "o.html", bbc(OTHER, "2026-10-13T11:00:00Z"))
        self.write("CNA", "z.html", cna(NOBEL_ZH, "2026-10-13T13:00:00Z"))
        self.db = self.tmp / "library.sqlite3"
        self.status = self.tmp / "status.json"
        index._last_sync.clear()
        index.sync(self.root, self.db, force=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, site, name, data):
        (self.root / site / name).write_bytes(data)

    def id_of(self, site, name):
        return index.find_by_file(self.db, site, name)["id"]

    def refresh(self, translate=None, **kwargs):
        return stories.refresh(self.db, translate=translate, status_path=self.status, **kwargs)

    def groups(self):
        with closing(index.connect(self.db)) as db:
            rows = db.execute("SELECT article_id, story_key FROM story_members").fetchall()
        out = {}
        for article_id, key in rows:
            out.setdefault(key, set()).add(article_id)
        return out


class RefreshTests(StoryTestCase):
    def test_english_titles_group_without_any_key(self):
        result = self.refresh(None)
        a, b, o, z = (self.id_of("BBC", "a.html"), self.id_of("BBC", "b.html"),
                      self.id_of("BBC", "o.html"), self.id_of("CNA", "z.html"))
        self.assertEqual(list(self.groups().values()), [{a, b}])
        self.assertEqual(result["pending"], 1)  # the Chinese headline waits for a key
        self.assertEqual(result["multi_outlet"], 0)
        info = status.read(self.status)["stories"]
        self.assertFalse(info["key_configured"])
        self.assertNotIn(o, set().union(*self.groups().values()))
        self.assertNotIn(z, set().union(*self.groups().values()))

    def test_translated_headline_joins_and_is_paid_for_once(self):
        fake = FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN})
        result = self.refresh(fake)
        z = self.id_of("CNA", "z.html")
        self.assertEqual(fake.calls, [[NOBEL_ZH]])
        self.assertEqual(result["translated"], 1)
        self.assertEqual(result["chars_today"], len(NOBEL_ZH))
        self.assertEqual(result["multi_outlet"], 1)
        group = next(g for g in self.groups().values() if z in g)
        self.assertEqual(len(group), 3)
        # A second run neither re-translates nor re-counts.
        again = self.refresh(fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(again["chars_today"], len(NOBEL_ZH))
        self.assertEqual(again["pending"], 0)

    def test_story_key_is_the_smallest_member_and_survives_new_members(self):
        self.refresh(FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN}))
        a = self.id_of("BBC", "a.html")
        self.assertIn(f"s{a}", self.groups())
        self.write("BBC", "c.html", bbc("Nobel economics prize for business cycle models won by two US scholars",
                                          "2026-10-13T15:00:00Z"))
        index.sync(self.root, self.db, force=True)
        self.refresh(None)
        self.assertEqual(len(self.groups()[f"s{a}"]), 4)

    def test_daily_budget_stops_translation(self):
        fake = FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN})
        result = self.refresh(fake, daily_chars=len(NOBEL_ZH) - 1)
        self.assertEqual(fake.calls, [])
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["chars_today"], 0)

    def test_budget_already_used_today_counts(self):
        fake = FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN})
        with closing(index.connect(self.db)) as db:
            index._write(db, [("INSERT INTO story_usage(day, chars) VALUES(?, ?)", (stories._today(), 7_990))])
        self.refresh(fake, daily_chars=8_000)
        self.assertEqual(fake.calls, [])

    def test_disabled_translation_still_groups_english(self):
        fake = FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN})
        result = self.refresh(fake, enabled=False)
        self.assertEqual(fake.calls, [])
        self.assertEqual(len(self.groups()), 1)
        self.assertEqual(result["pending"], 1)

    def test_provider_error_is_recorded_and_titles_wait(self):
        fake = FakeTranslator(error=TranslationError("翻訳サービスの利用上限に達しました。"))
        result = self.refresh(fake)
        self.assertIn("上限", result["error"])
        self.assertEqual(result["pending"], 1)
        self.assertEqual(len(self.groups()), 1)  # English groups are still built
        self.assertIn("上限", status.read(self.status)["stories"]["error"])
        # Unexpected exceptions are recorded by type only (their text could carry a response body).
        result = self.refresh(FakeTranslator(error=RuntimeError("secret response body")))
        self.assertEqual(result["error"], "RuntimeError")

    def test_retitled_article_is_pivoted_again(self):
        self.refresh(None)
        self.write("BBC", "o.html", bbc("Two US scholars share Nobel economics prize for business cycle models",
                                          "2026-10-13T11:00:00Z"))
        index.sync(self.root, self.db, force=True)
        self.refresh(None)
        o = self.id_of("BBC", "o.html")
        with closing(index.connect(self.db)) as db:
            pivot = db.execute("SELECT pivot FROM story_items WHERE article_id = ?", (o,)).fetchone()[0]
        self.assertIn("Nobel", pivot)
        self.assertTrue(any(o in g for g in self.groups().values()))

    def test_deleted_article_leaves_no_story_rows(self):
        self.refresh(FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN}))
        z = self.id_of("CNA", "z.html")
        (self.root / "CNA" / "z.html").unlink()
        index.sync(self.root, self.db, force=True)
        with closing(index.connect(self.db)) as db:
            for table in ("story_items", "story_members"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE article_id = ?", (z,)).fetchone()[0], 0)

    def test_queries(self):
        self.refresh(FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN}))
        multi = stories.list_stories(self.db, scope="multi")
        self.assertEqual(multi["total"], 1)
        story = multi["stories"][0]
        self.assertEqual((story["count"], story["outlets"]), (3, 2))
        detail = stories.get_story(self.db, story["key"])
        self.assertEqual([source for source, _ in detail["columns"]], ["BBC", "CNA"])
        a = self.id_of("BBC", "a.html")
        related = stories.related(self.db, a)
        self.assertEqual(len(related["members"]), 2)
        self.assertNotIn(a, [m["id"] for m in related["members"]])
        self.assertIsNone(stories.related(self.db, self.id_of("BBC", "o.html")))
        self.assertIsNone(stories.get_story(self.db, "s999999"))
        self.assertEqual(stories.list_stories(self.db, page=99)["stories"], [])

    def test_pipeline_without_key_uses_english_only(self):
        result = pipeline.refresh_stories(self.db, self.status)  # conftest: empty in-memory keyring
        self.assertEqual(result["pending"], 1)
        self.assertFalse(status.read(self.status)["stories"]["key_configured"])


class WebTests(StoryTestCase):
    def setUp(self):
        super().setUp()
        self.refresh(FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN}))
        self.app = create_app(self.root, self.db, start_background=False, secret_key=b"k" * 32)
        self.app.config["STATUS_PATH"] = self.status
        self.client = self.app.test_client()
        self.key = stories.list_stories(self.db)["stories"][0]["key"]

    def csrf(self):
        self.client.get("/", headers=H)
        with self.client.session_transaction() as session:
            return session["csrf"]

    def test_stories_pages(self):
        page = self.client.get("/stories", headers=H)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("2 媒体 · 3 本", html)
        self.assertIn(NOBEL_ZH, html)
        self.assertIn(NOBEL_ZH_EN, html)  # the English pivot is shown under a non-English title
        self.assertIn('aria-current="page">読み比べ', html)
        self.assertEqual(page.headers["Cache-Control"], "no-cache")
        all_scope = self.client.get("/stories?scope=all", headers=H).get_data(as_text=True)
        self.assertIn('chip active" href="/stories?scope=all"', all_scope)
        self.assertNotIn(OTHER, all_scope)  # a lone article is not a group
        detail = self.client.get(f"/stories/{self.key}", headers=H)
        self.assertEqual(detail.status_code, 200)
        text = detail.get_data(as_text=True)
        self.assertEqual(text.count('class="story-column"'), 2)
        self.assertEqual(self.client.get("/stories/s999999", headers=H).status_code, 404)
        self.assertEqual(self.client.get("/stories?page=abc", headers=H).status_code, 200)

    def test_article_page_lists_related_outside_the_reading_text(self):
        a = self.id_of("BBC", "a.html")
        html = self.client.get(f"/a/{a}", headers=H).get_data(as_text=True)
        self.assertIn("同じニュースの他の記事", html)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        # The translation script pairs #reading-content's paragraphs with translations: nothing else may live there.
        self.assertIsNotNone(soup.select_one("#reading-content"))
        self.assertIsNone(soup.select_one("#reading-content .related"))
        self.assertEqual(len(soup.select("#reading-content p")), 1)
        self.assertIn(f"/stories/{self.key}", html)
        other = self.client.get(f"/a/{self.id_of('BBC', 'o.html')}", headers=H).get_data(as_text=True)
        self.assertNotIn("同じニュースの他の記事", other)

    def test_refresh_api_requires_csrf_and_reports_status(self):
        self.assertEqual(self.client.post("/api/stories/refresh", headers=H).status_code, 400)  # central CSRF check
        response = self.client.post("/api/stories/refresh", headers={**H, "X-CSRF-Token": self.csrf()})
        self.assertEqual(response.status_code, 202)
        from newsreader.web import stories_view
        stories_view._refresh_thread.join(30)
        info = self.client.get("/api/stories/status", headers=H).get_json()
        self.assertFalse(info["running"])
        self.assertIn("groups", info)

    def test_settings_section_saves_and_validates(self):
        token = self.csrf()
        form = {"csrf": token, "provider": "azure", "deepl_endpoint": "auto", "azure_region": "",
                "network_bind_host": "", "network_port": "", "naver_search_query": "뉴스"}
        page = self.client.post("/translation-settings", headers=H,
                                data={**form, "stories_daily_chars": "１２，０００"})  # full-width + comma
        self.assertIn("設定を保存しました", page.get_data(as_text=True))
        self.assertEqual(settings.get_story_settings(), {"stories_enabled": False, "stories_daily_chars": 12_000})
        page = self.client.post("/translation-settings", headers=H,
                                data={**form, "stories_enabled": "1", "stories_daily_chars": "50"})
        self.assertIn("500", page.get_data(as_text=True))
        self.assertEqual(settings.get_story_settings()["stories_daily_chars"], 12_000)  # unchanged
        # A form without the section (older page, scripts) keeps the stored values.
        self.client.post("/translation-settings", headers=H, data={**form, "provider": "deepl"})
        stored = json.loads(settings.SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual((stored["provider"], stored["stories_daily_chars"]), ("deepl", 12_000))
        self.assertIn('id="stories-settings"', self.client.get("/translation-settings", headers=H).get_data(as_text=True))

    def tearDown(self):
        settings.SETTINGS_PATH.unlink(missing_ok=True)
        super().tearDown()


class NameEvidenceTests(unittest.TestCase):
    """D-21: a weak pair must share a name-like token; common words alone do not join events."""

    def group(self, *headlines, hours=1):
        from datetime import datetime, timedelta, timezone
        from newsreader.library import stories_core as core
        start = datetime(2026, 10, 13, tzinfo=timezone.utc)
        items = {n: (core.features(h), start + timedelta(hours=hours * n)) for n, h in enumerate(headlines)}
        shapes = {n: core.shapes(h) for n, h in enumerate(headlines)}
        return [sorted(c) for c in core.cluster(items, shapes)], [sorted(c) for c in core.cluster(items)]

    FILLER = ("Heavy snow closes mountain passes across the Alps", "Bank of England holds interest rates",
              "Wildfire forces evacuations in California", "Football club appoints new manager")

    def test_common_words_alone_do_not_join(self):
        # Two different events that share only "pupil / teacher / school" (a real false pair under D-07).
        with_rule, without = self.group("Pupil arrested after two teachers shot at French school",
                                        "At least 33 pupils and teacher killed after school roof collapses in Pakistan",
                                        *self.FILLER)
        self.assertEqual(with_rule, [])
        self.assertEqual(without, [[0, 1]])  # the handover's D-07 behaviour, kept when no shapes are given

    def test_shared_name_or_quoted_term_still_joins(self):
        self.assertEqual(self.group("Albanian protests turn violent: What we know",
                                    "Anti-government protesters battle with police in Albania")[0], [[0, 1]])
        self.assertEqual(self.group("Delhi sees more protests as freed 'cockroach' group's leaders vow to fight on",
                                    "Police detain 'cockroach' leaders after protest")[0], [[0, 1]])

    def test_title_case_headlines_do_not_make_names(self):
        from newsreader.library import stories_core as core
        self.assertTrue(core.is_title_case("Riyadh Airport Attacks Again Cause 1 Death and 11 Injuries"))
        self.assertFalse(core.is_title_case("Trump proposes renaming the Atlantic Ocean or Pacific Ocean"))
        names = core.name_tokens({1: core.shapes("White House Press Secretary Reportedly Fired Five Days After Taking Office"),
                                  2: core.shapes("Protests turn violent in the capital"),
                                  3: core.shapes("Police say protests turn into riots")})
        self.assertNotIn("protes", names)
        self.assertNotIn("fire", names)
        # A quoted term keeps counting as a name even in a Title Case headline.
        self.assertIn("cockro", core.name_tokens({1: core.shapes("Police Detain 'Cockroach' Party Leaders In Delhi"),
                                                  2: core.shapes("the cockroach is an insect")}))

    def test_names_are_decided_over_the_whole_corpus(self):
        from newsreader.library import stories_core as core
        names = core.name_tokens({1: core.shapes("Pupil arrested after teachers shot"),
                                  2: core.shapes("Ten pupils hurt as school roof collapses in Pakistan"),
                                  3: core.shapes("Pope to be discharged after surgery")})
        self.assertIn("pakist", names)
        self.assertIn("pope", names)          # capitalised and never seen in lower case
        self.assertNotIn("pupil", names)      # seen in lower case elsewhere
        self.assertNotIn("school", names)


class LiveBbcEvaluation(unittest.TestCase):
    """Secondary check on 119 live English headlines (labels NOT blind, see the fixture's provenance)."""

    #: Measured 2026-10-19 with D-21: precision 0.741, recall 0.645, partner 0.77 (D-07 alone: 0.588 / 0.645 / 0.77).
    FLOORS = {"precision": 0.73, "recall": 0.64, "partner": 0.76}

    def test_against_labels(self):
        from newsreader.library import stories_core as core
        data = json.loads((LABELS.parent / "story_labels_live_bbc.json").read_text(encoding="utf-8"))
        articles = {n: a for n, a in enumerate(data["articles"])}
        items = {n: (core.features(a["title"]), core.parse_utc(a["published"])) for n, a in articles.items()}
        shapes = {n: core.shapes(a["title"]) for n, a in articles.items()}
        key = {n: a["key"] for n, a in articles.items()}
        pred = {tuple(sorted((key[a], key[b]))) for c in core.cluster(items, shapes) for a, b in itertools.combinations(c, 2)}
        pred -= {tuple(sorted(p)) for p in data["ambiguous"]}
        true = {tuple(sorted(p)) for members in data["groups"].values() for p in itertools.combinations(members, 2)}
        correct = pred & true
        labelled = [k for members in data["groups"].values() for k in members]
        partner = sum(any(tuple(sorted((i, j))) in correct for j in labelled if j != i) for i in labelled) / len(labelled)
        measured = {"precision": len(correct) / len(pred), "recall": len(correct) / len(true), "partner": partner}
        print("\nlive BBC evaluation:", {k: round(v, 3) for k, v in measured.items()})
        for name, floor in self.FLOORS.items():
            self.assertGreaterEqual(measured[name], floor, name)


@unittest.skipUnless(os.environ.get("NEWS_REALDATA"), "NEWS_REALDATA not set")
class LabelledEvaluation(unittest.TestCase):
    """Pairwise agreement with labels written before any clustering run (tests/fixtures/story_labels.json)."""

    #: First measured run (2026-10-19, parameters D-07): precision 0.653, recall 0.528,
    #: partner 0.78, cross-outlet partner 0.75 — unchanged by D-21. Floors leave room only for rounding.
    FLOORS = {"precision": 0.64, "recall": 0.52, "partner": 0.76, "cross_partner": 0.72}

    def test_against_labels(self):
        import story_glosses
        tmp = Path(tempfile.mkdtemp())
        try:
            db = tmp / "library.sqlite3"
            index.sync(Path(os.environ["NEWS_REALDATA"]), db, force=True)
            stories.refresh(db, translate=story_glosses.translate, daily_chars=200_000, status_path=tmp / "s.json")
            with closing(index.connect(db)) as c:
                ids = {(r["source"], r["filename"]): r["id"] for r in c.execute("SELECT id, source, filename FROM articles")}
                source = {r["id"]: r["source"] for r in c.execute("SELECT id, source FROM articles")}
                predicted = {r[0]: r[1] for r in c.execute("SELECT article_id, story_key FROM story_members")}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        truth = {ids[(m["source"], m["filename"])]: group
                 for group, members in json.loads(LABELS.read_text(encoding="utf-8"))["groups"].items() for m in members}
        self.assertEqual(len(truth), 50)

        def pairs(assign):
            by = {}
            for article, key in assign.items():
                by.setdefault(key, []).append(article)
            return {tuple(sorted(p)) for members in by.values() for p in itertools.combinations(members, 2)}

        pred, true = pairs(predicted), pairs(truth)
        correct = pred & true
        labelled = list(truth)

        def linked(i, cross):
            return any(tuple(sorted((i, j))) in correct and (not cross or source[i] != source[j]) for j in labelled if j != i)

        cross = [i for i in labelled if any(truth[j] == truth[i] and source[j] != source[i] for j in labelled)]
        measured = {"precision": len(correct) / len(pred), "recall": len(correct) / len(true),
                    "partner": sum(linked(i, False) for i in labelled) / len(labelled),
                    "cross_partner": sum(linked(i, True) for i in cross) / len(cross)}
        print("\nstory evaluation:", {k: round(v, 3) for k, v in measured.items()},
              f"pairs predicted={len(pred)} true={len(true)} correct={len(correct)}")
        for name, floor in self.FLOORS.items():
            self.assertGreaterEqual(measured[name], floor, name)


if __name__ == "__main__":
    unittest.main()
