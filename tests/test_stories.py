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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from datetime import UTC

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
        a, b, o, z = (
            self.id_of("BBC", "a.html"),
            self.id_of("BBC", "b.html"),
            self.id_of("BBC", "o.html"),
            self.id_of("CNA", "z.html"),
        )
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
        self.write(
            "BBC",
            "c.html",
            bbc("Nobel economics prize for business cycle models won by two US scholars", "2026-10-13T15:00:00Z"),
        )
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
        self.write(
            "BBC",
            "o.html",
            bbc("Two US scholars share Nobel economics prize for business cycle models", "2026-10-13T11:00:00Z"),
        )
        index.sync(self.root, self.db, force=True)
        self.refresh(None)
        o = self.id_of("BBC", "o.html")
        with closing(index.connect(self.db)) as db:
            pivot = db.execute("SELECT pivot FROM story_items WHERE article_id = ?", (o,)).fetchone()[0]
        self.assertIn("Nobel", pivot)
        self.assertTrue(any(o in g for g in self.groups().values()))

    def test_groups_older_than_the_window_never_shrink_to_one_article(self):
        self.refresh(FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN}))
        a = self.id_of("BBC", "a.html")
        from unittest import mock

        # Only the two newest articles are reclustered: the oldest member of the group is left behind.
        with mock.patch.object(stories, "MAX_CLUSTER_ARTICLES", 2):
            self.refresh(None)
        counts = [len(g) for g in self.groups().values()]
        self.assertTrue(all(n >= 2 for n in counts), counts)
        self.assertFalse(any(a in g for g in self.groups().values()))  # left alone: no longer in a "group"

    def test_deleted_article_leaves_no_story_rows(self):
        self.refresh(FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN}))
        z = self.id_of("CNA", "z.html")
        (self.root / "CNA" / "z.html").unlink()
        index.sync(self.root, self.db, force=True)
        with closing(index.connect(self.db)) as db:
            for table in ("story_items", "story_members"):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM {table} WHERE article_id = ?", (z,)).fetchone()[0], 0
                )

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

    def test_list_preview_puts_other_outlets_first(self):
        # newest first, as _members returns them; the lead is a BBC article
        group = [
            {"id": 1, "source": "BBC"},
            {"id": 2, "source": "BBC"},
            {"id": 3, "source": "BBC"},
            {"id": 4, "source": "CNA"},
            {"id": 5, "source": "NAVER NEWS"},
            {"id": 6, "source": "CNA"},
        ]
        self.assertEqual([m["id"] for m in stories.preview(group, 3)], [4, 5, 1])  # other outlets lead
        self.assertEqual([m["id"] for m in stories.preview(group, 4)], [1, 5, 2])
        self.assertEqual([m["id"] for m in stories.preview(group, 4, limit=9)], [1, 5, 2, 3, 6])
        self.assertEqual(stories.preview(group[:1], 1), [])
        self.refresh(FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN}))
        story = stories.list_stories(self.db)["stories"][0]
        self.assertNotIn(story["lead_id"], [m["id"] for m in story["preview"]])
        self.assertEqual(len(story["preview"]), 2)

    def test_pipeline_without_key_uses_english_only(self):
        result = pipeline.refresh_stories(self.db, self.status)  # conftest: empty in-memory keyring
        self.assertEqual(result["pending"], 1)
        self.assertFalse(status.read(self.status)["stories"]["key_configured"])

    def test_a_start_does_not_regroup_groups_that_are_current(self):
        nothing = {"added": 0, "updated": 0, "removed": 0}
        with mock.patch.object(pipeline, "refresh_stories", wraps=pipeline.refresh_stories) as regroup:
            pipeline.after_sync(nothing, self.db, self.status)  # never grouped yet
            self.assertEqual(regroup.call_count, 1)
            self.assertEqual(status.read(self.status)["stories"]["code"], pipeline.GROUPING_CODE)
            pipeline.after_sync(nothing, self.db, self.status)  # the next start: nothing to do
            self.assertEqual(regroup.call_count, 1)
            pipeline.after_sync({**nothing, "added": 1}, self.db, self.status)  # articles changed
            self.assertEqual(regroup.call_count, 2)
            status.update("stories", {"code": "older code"}, path=self.status)  # a deploy changed it
            pipeline.after_sync(nothing, self.db, self.status)
            self.assertEqual(regroup.call_count, 3)
            later = "9999-01-01T00:00:00Z"  # indexed after the last grouping (a failed regroup)
            status.update("library", {"synced": later}, path=self.status)
            pipeline.after_sync(nothing, self.db, self.status)
            self.assertEqual(regroup.call_count, 4)


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
        self.assertIn(
            '<span class="badge" data-lang="en">BBC ×2</span><span class="badge" data-lang="zh">CNA</span>', html
        )
        self.assertIn("3 本を並べて読む", html)
        self.assertIn(NOBEL_ZH, html.replace("<wbr>", ""))
        self.assertEqual(html.count("data-gloss-id="), 3)  # headline + the two other members
        self.assertIn('aria-current="page">読み比べ', html)
        self.assertEqual(page.headers["Cache-Control"], "no-cache")
        all_scope = self.client.get("/stories?scope=all", headers=H).get_data(as_text=True)
        self.assertIn('chip active" href="/stories?scope=all"', all_scope)
        self.assertNotIn(OTHER, all_scope)  # a lone article is not a group
        detail = self.client.get(f"/stories/{self.key}", headers=H)
        self.assertEqual(detail.status_code, 200)
        text = detail.get_data(as_text=True)
        self.assertEqual(text.count('class="story-column"'), 2)
        self.assertEqual(text.count('class="text-button exclude-button"'), 6)  # 3 articles x (by outlet + timeline)
        self.assertIn("最初の報道", text)
        self.assertEqual(detail.headers["Cache-Control"], "no-cache")
        self.assertEqual(self.client.get("/stories/s999999", headers=H).status_code, 404)
        self.assertEqual(self.client.get("/stories?page=abc", headers=H).status_code, 200)

    def test_search_keeps_the_groups_with_a_matching_article(self):
        lead = stories.list_stories(self.db)["stories"][0]["lead_title"]
        word = lead.split()[0]
        self.assertEqual(stories.list_stories(self.db, query=word)["total"], 1)
        self.assertEqual(stories.list_stories(self.db, query="zzzqqq")["total"], 0)
        english = stories.list_stories(self.db, query="xx", pivot_words=index.search_words(NOBEL_ZH_EN))
        self.assertEqual(english["total"], 1)  # found through the English headline of the CNA article
        html = self.client.get("/stories?q=zzzqqq", headers=H).get_data(as_text=True)
        self.assertIn("に当たるグループはありません", html)
        self.assertIn("検索をやめる", html)

    def test_reader_correction_takes_an_article_out_for_good(self):
        z = self.id_of("CNA", "z.html")
        token = self.csrf()
        response = self.client.post(
            "/api/stories/exclude", headers={**H, "X-CSRF-Token": token}, json={"id": z, "key": self.key}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["redirect"].startswith("/stories/"))
        self.assertIsNone(stories.related(self.db, z))
        # A later regrouping (even with the headline translated) keeps them apart.
        self.refresh(FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN}))
        self.assertIsNone(stories.related(self.db, z))
        self.assertEqual(stories.exclusion_count(self.db), 2)
        self.assertIn("外した組: 2 組", self.client.get("/status", headers=H).get_data(as_text=True))
        # Not a member any more: refused. Undo brings it back.
        again = self.client.post(
            "/api/stories/exclude", headers={**H, "X-CSRF-Token": token}, json={"id": z, "key": self.key}
        )
        self.assertEqual(again.status_code, 404)
        self.assertEqual(
            self.client.post("/api/stories/exclusions/clear", headers={**H, "X-CSRF-Token": token}).status_code, 200
        )
        self.assertEqual(stories.exclusion_count(self.db), 0)
        self.assertIsNotNone(stories.related(self.db, z))
        for bad in ({"id": "1", "key": self.key}, {"id": z}, {"id": True, "key": self.key}):
            self.assertEqual(
                self.client.post("/api/stories/exclude", headers={**H, "X-CSRF-Token": token}, json=bad).status_code,
                400,
            )

    def test_reader_join_puts_a_similar_article_into_the_group_for_good(self):
        o, z = self.id_of("BBC", "o.html"), self.id_of("CNA", "z.html")
        token = self.csrf()
        self.assertEqual(
            self.client.post("/api/stories/join", headers=H, json={"id": z, "other": o}).status_code, 400
        )  # no CSRF
        response = self.client.post(
            "/api/stories/join", headers={**H, "X-CSRF-Token": token}, json={"id": z, "other": o}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["redirect"], f"/stories/{self.key}")
        self.assertEqual(stories.related(self.db, o)["key"], self.key)
        self.refresh(FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN}))  # a later regrouping keeps it
        self.assertEqual(stories.related(self.db, o)["key"], self.key)
        self.assertIn("まとめた組: 1 組", self.client.get("/status", headers=H).get_data(as_text=True))
        # "別のニュース" afterwards wins, and forgets the join between those two
        self.client.post("/api/stories/exclude", headers={**H, "X-CSRF-Token": token}, json={"id": o, "key": self.key})
        self.assertIsNone(stories.related(self.db, o))
        self.assertEqual(stories.join_count(self.db), 0)
        # joining again replaces the exclusion of exactly that pair, but the others still keep it out
        refused = self.client.post(
            "/api/stories/join", headers={**H, "X-CSRF-Token": token}, json={"id": z, "other": o}
        )
        self.assertEqual(refused.status_code, 409)
        self.assertIn("別のニュース", refused.get_json()["error"])
        self.assertIsNone(stories.related(self.db, o))
        self.assertEqual(
            self.client.post("/api/stories/exclusions/clear", headers={**H, "X-CSRF-Token": token}).status_code, 200
        )
        self.assertEqual(stories.related(self.db, o)["key"], self.key)
        self.assertEqual(
            self.client.post("/api/stories/joins/clear", headers={**H, "X-CSRF-Token": token}).get_json()["cleared"], 1
        )
        self.assertIsNone(stories.related(self.db, o))
        for bad in ({"id": z}, {"id": z, "other": "1"}, {"id": True, "other": o}, {"id": z, "other": z}):
            code = self.client.post("/api/stories/join", headers={**H, "X-CSRF-Token": token}, json=bad).status_code
            self.assertIn(code, (400, 404))
        self.assertEqual(
            self.client.post(
                "/api/stories/join", headers={**H, "X-CSRF-Token": token}, json={"id": z, "other": 999999}
            ).status_code,
            404,
        )

    def test_similar_articles_offer_the_join_button(self):
        o, z = self.id_of("BBC", "o.html"), self.id_of("CNA", "z.html")
        fake = [{"id": z, "source": "CNA", "title": NOBEL_ZH, "published": "2026-10-13T13:00:00Z", "score": 0.3}]
        with mock.patch.object(stories, "similar", lambda db, article_id, limit=5: fake):
            page = self.client.get(f"/a/{o}", headers=H).get_data(as_text=True)
        self.assertIn(f'class="text-button join-button" data-join-id="{o}" data-join-other="{z}"', page)

    def test_openings_are_translated_for_comparison(self):
        from newsreader.services import cache
        from newsreader.services import translation as ts

        calls = []

        def fake(provider, texts, target):
            calls.append(list(texts))
            return [f"[{target}] {t}" for t in texts]

        token = self.csrf()
        ids = [self.id_of("BBC", "a.html"), self.id_of("CNA", "z.html")]
        with (
            mock.patch.object(ts, "_translate_batch", fake),
            mock.patch.object(ts, "active_provider", lambda: "deepl"),
            mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3"),
        ):
            leads = self.client.post(
                "/api/translate-leads", headers={**H, "X-CSRF-Token": token}, json={"ids": ids, "target": "ja"}
            ).get_json()["leads"]
            self.assertEqual(leads[str(ids[0])]["original"], f"Body of {NOBEL_A}.")
            self.assertEqual(leads[str(ids[0])]["translation"], f"[ja] Body of {NOBEL_A}.")
            self.client.post(
                "/api/translate-leads", headers={**H, "X-CSRF-Token": token}, json={"ids": ids, "target": "ja"}
            )
        self.assertEqual(sum(len(c) for c in calls), 2)  # cached the second time
        bad = self.client.post(
            "/api/translate-leads",
            headers={**H, "X-CSRF-Token": token},
            json={"ids": list(range(1, 20)), "target": "ja"},
        )
        self.assertEqual(bad.status_code, 400)

    def test_articles_can_be_read_in_another_outlets_language(self):
        from newsreader.services import cache
        from newsreader.services import translation as ts

        detail = stories.get_story(self.db, self.key)
        self.assertEqual(
            [(l["target"], l["outlets"], l["count"]) for l in detail["languages"]],
            [("en", ["BBC"], 2), ("zh-hant", ["CNA"], 1)],
        )
        html = self.client.get(f"/stories/{self.key}", headers=H).get_data(as_text=True)
        self.assertIn('data-align="en"', html)
        self.assertIn('data-align="zh-hant"', html)
        self.assertEqual(html.count('class="cmp-tr"'), 6)  # hidden until asked (3 articles x 2 views)
        calls = []

        def fake(provider, texts, target):
            calls.append((target, list(texts)))
            return [f"[{target}] {t}" for t in texts]

        token = self.csrf()
        bbc, cna = self.id_of("BBC", "a.html"), self.id_of("CNA", "z.html")
        with (
            mock.patch.object(ts, "_translate_batch", fake),
            mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3"),
        ):
            items = self.client.post(
                "/api/compare-translate", headers={**H, "X-CSRF-Token": token}, json={"ids": [bbc, cna], "target": "en"}
            ).get_json()["items"]
            self.assertEqual(list(items), [str(cna)])  # the BBC article is English already
            self.assertEqual(items[str(cna)]["title"], f"[en] {NOBEL_ZH}")
            self.assertTrue(items[str(cna)]["lead"].startswith("[en] "))
            other = self.client.post(
                "/api/compare-translate",
                headers={**H, "X-CSRF-Token": token},
                json={"ids": [bbc, cna], "target": "zh-hant"},
            ).get_json()["items"]
            self.assertEqual(list(other), [str(bbc)])
        self.assertEqual({target for target, _ in calls}, {"en", "zh-hant"})
        self.assertEqual(
            self.client.post(
                "/api/compare-translate", headers={**H, "X-CSRF-Token": token}, json={"ids": [bbc], "target": "xx"}
            ).status_code,
            400,
        )

    def test_article_offers_the_others_in_its_own_language(self):
        bbc, cna = self.id_of("BBC", "a.html"), self.id_of("CNA", "z.html")
        html = self.client.get(f"/a/{bbc}", headers=H).get_data(as_text=True)
        self.assertIn(f"/stories/{self.key}?align=en", html)
        self.assertIn("ほかの記事をEnglishにそろえて比べる", html)
        self.assertIn(
            f"/stories/{self.key}?align=zh-hant", self.client.get(f"/a/{cna}", headers=H).get_data(as_text=True)
        )

    def test_list_cards_mark_articles_reported_by_several_outlets(self):
        html = self.client.get("/", headers=H).get_data(as_text=True)
        self.assertEqual(html.count("読み比べ 2媒体"), 3)  # the two BBC Nobel articles and the CNA one
        self.assertEqual(stories.badges(self.db, [self.id_of("BBC", "o.html")]), {})
        self.assertEqual(stories.badges(self.db, []), {})

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

    def test_status_page_shows_the_grouping(self):
        html = self.client.get("/status", headers=H).get_data(as_text=True)
        self.assertIn("<h2>読み比べ</h2>", html)
        self.assertIn("1 件（複数の媒体 1 件）", html)
        self.assertIn(f"本日 {len(NOBEL_ZH)} / 8,000 文字", html)

    def test_version_time_is_shown_in_local_time(self):
        from datetime import datetime

        from newsreader.web.status_view import local_version

        expected = datetime(2026, 10, 18, 18, 43, tzinfo=UTC).astimezone().strftime("%Y-%m-%d %H:%M")
        self.assertEqual(local_version("phase-9 (abc1234) 2026-10-18 18:43 +0000"), f"phase-9 (abc1234) {expected}")
        self.assertEqual(local_version("dev"), "dev")
        self.assertEqual(local_version(""), "")

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
        form = {
            "csrf": token,
            "provider": "azure",
            "deepl_endpoint": "auto",
            "azure_region": "",
            "network_bind_host": "",
            "network_port": "",
            "naver_search_query": "뉴스",
        }
        page = self.client.post(
            "/translation-settings", headers=H, data={**form, "stories_daily_chars": "１２，０００"}
        )  # full-width + comma
        self.assertIn("設定を保存しました", page.get_data(as_text=True))
        self.assertEqual(settings.get_story_settings(), {"stories_enabled": False, "stories_daily_chars": 12_000})
        page = self.client.post(
            "/translation-settings", headers=H, data={**form, "stories_enabled": "1", "stories_daily_chars": "50"}
        )
        self.assertIn("500", page.get_data(as_text=True))
        self.assertEqual(settings.get_story_settings()["stories_daily_chars"], 12_000)  # unchanged
        # A form without the section (older page, scripts) keeps the stored values.
        self.client.post("/translation-settings", headers=H, data={**form, "azure_region": "japaneast"})
        stored = json.loads(settings.SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual((stored["azure_region"], stored["stories_daily_chars"]), ("japaneast", 12_000))
        self.assertIn(
            'id="stories-settings"', self.client.get("/translation-settings", headers=H).get_data(as_text=True)
        )

    def tearDown(self):
        settings.SETTINGS_PATH.unlink(missing_ok=True)
        super().tearDown()


class NameEvidenceTests(unittest.TestCase):
    """D-21: a weak pair must share a name-like token; common words alone do not join events."""

    def group(self, *headlines, hours=1):
        from datetime import datetime, timedelta

        from newsreader.library import stories_core as core

        start = datetime(2026, 10, 13, tzinfo=UTC)
        items = {n: (core.features(h), start + timedelta(hours=hours * n)) for n, h in enumerate(headlines)}
        shapes = {n: core.shapes(h) for n, h in enumerate(headlines)}
        return [sorted(c) for c in core.cluster(items, shapes)], [sorted(c) for c in core.cluster(items)]

    FILLER = (
        "Heavy snow closes mountain passes across the Alps",
        "Bank of England holds interest rates",
        "Wildfire forces evacuations in California",
        "Football club appoints new manager",
    )

    def test_common_words_alone_do_not_join(self):
        # Two different events that share only "pupil / teacher / school" (a real false pair under D-07).
        with_rule, without = self.group(
            "Pupil arrested after two teachers shot at French school",
            "At least 33 pupils and teacher killed after school roof collapses in Pakistan",
            *self.FILLER,
        )
        self.assertEqual(with_rule, [])
        self.assertEqual(without, [[0, 1]])  # the handover's D-07 behaviour, kept when no shapes are given

    def test_shared_name_or_quoted_term_still_joins(self):
        self.assertEqual(
            self.group(
                "Albanian protests turn violent: What we know",
                "Anti-government protesters battle with police in Albania",
            )[0],
            [[0, 1]],
        )
        self.assertEqual(
            self.group(
                "Delhi sees more protests as freed 'cockroach' group's leaders vow to fight on",
                "Police detain 'cockroach' leaders after protest",
            )[0],
            [[0, 1]],
        )

    def test_title_case_headlines_do_not_make_names(self):
        from newsreader.library import stories_core as core

        self.assertTrue(core.is_title_case("Riyadh Airport Attacks Again Cause 1 Death and 11 Injuries"))
        self.assertFalse(core.is_title_case("Trump proposes renaming the Atlantic Ocean or Pacific Ocean"))
        names = core.name_tokens(
            {
                1: core.shapes("White House Press Secretary Reportedly Fired Five Days After Taking Office"),
                2: core.shapes("Protests turn violent in the capital"),
                3: core.shapes("Police say protests turn into riots"),
            }
        )
        self.assertNotIn("protes", names)
        self.assertNotIn("fire", names)
        # A quoted term keeps counting as a name even in a Title Case headline.
        self.assertIn(
            "cockro",
            core.name_tokens(
                {
                    1: core.shapes("Police Detain 'Cockroach' Party Leaders In Delhi"),
                    2: core.shapes("the cockroach is an insect"),
                }
            ),
        )

    def test_forbidden_pairs_never_share_a_cluster(self):
        from datetime import datetime, timedelta

        from newsreader.library import stories_core as core

        start = datetime(2026, 10, 13, tzinfo=UTC)
        heads = [NOBEL_A, NOBEL_B, NOBEL_ZH_EN, *self.FILLER]
        items = {n: (core.features(h), start + timedelta(hours=n)) for n, h in enumerate(heads)}
        shapes = {n: core.shapes(h) for n, h in enumerate(heads)}
        self.assertEqual([sorted(c) for c in core.cluster(items, shapes)], [[0, 1, 2]])
        clusters = [sorted(c) for c in core.cluster(items, shapes, {(0, 2)})]
        self.assertTrue(all(not {0, 2} <= set(c) for c in clusters), clusters)

    def test_names_are_decided_over_the_whole_corpus(self):
        from newsreader.library import stories_core as core

        names = core.name_tokens(
            {
                1: core.shapes("Pupil arrested after teachers shot"),
                2: core.shapes("Ten pupils hurt as school roof collapses in Pakistan"),
                3: core.shapes("Pope to be discharged after surgery"),
            }
        )
        self.assertIn("pakist", names)
        self.assertIn("pope", names)  # capitalised and never seen in lower case
        self.assertNotIn("pupil", names)  # seen in lower case elsewhere
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
        pred = {
            tuple(sorted((key[a], key[b])))
            for c in core.cluster(items, shapes)
            for a, b in itertools.combinations(c, 2)
        }
        pred -= {tuple(sorted(p)) for p in data["ambiguous"]}
        true = {tuple(sorted(p)) for members in data["groups"].values() for p in itertools.combinations(members, 2)}
        correct = pred & true
        labelled = [k for members in data["groups"].values() for k in members]
        partner = sum(any(tuple(sorted((i, j))) in correct for j in labelled if j != i) for i in labelled) / len(
            labelled
        )
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
                ids = {
                    (r["source"], r["filename"]): r["id"]
                    for r in c.execute("SELECT id, source, filename FROM articles")
                }
                source = {r["id"]: r["source"] for r in c.execute("SELECT id, source FROM articles")}
                predicted = {r[0]: r[1] for r in c.execute("SELECT article_id, story_key FROM story_members")}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        truth = {
            ids[(m["source"], m["filename"])]: group
            for group, members in json.loads(LABELS.read_text(encoding="utf-8"))["groups"].items()
            for m in members
        }
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
            return any(
                tuple(sorted((i, j))) in correct and (not cross or source[i] != source[j]) for j in labelled if j != i
            )

        cross = [i for i in labelled if any(truth[j] == truth[i] and source[j] != source[i] for j in labelled)]
        measured = {
            "precision": len(correct) / len(pred),
            "recall": len(correct) / len(true),
            "partner": sum(linked(i, False) for i in labelled) / len(labelled),
            "cross_partner": sum(linked(i, True) for i in cross) / len(cross),
        }
        print(
            "\nstory evaluation:",
            {k: round(v, 3) for k, v in measured.items()},
            f"pairs predicted={len(pred)} true={len(true)} correct={len(correct)}",
        )
        for name, floor in self.FLOORS.items():
            self.assertGreaterEqual(measured[name], floor, name)


class HyphenTests(unittest.TestCase):
    def test_hyphenated_words(self):
        from newsreader.library import stories_core as core

        self.assertEqual(
            set(core.features("Brazil's run-off vote")) & set(core.features("Brazil runoff")), {"brazil", "runoff"}
        )
        self.assertIn("iran", core.features("Yemen's Iran-backed rebels"))
        self.assertEqual(set(core.features("Moscow tight-lipped on energy")), {"moscow", "tightl", "energy"})
        self.assertEqual(set(core.features("US-Japan drills")), {"us", "japan", "drill"})


class JoinTests(unittest.TestCase):
    """同じニュース (D-72): reader joins merge groups unless a "別のニュース" pair would end up together."""

    def test_apply_joins(self):
        clusters = [{1: 0.5, 2: 0.5}, {3: 0.4, 4: 0.4}]
        self.assertEqual(stories._apply_joins(clusters, [], set()), clusters)
        merged = stories._apply_joins(clusters, [(2, 3)], set())
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0]), {1, 2, 3, 4})
        self.assertEqual(stories._apply_joins(clusters, [(2, 3)], {(1, 4)}), clusters)  # would join a separated pair
        lone = stories._apply_joins(clusters, [(5, 6), (6, 1)], set())
        self.assertEqual(sorted(sorted(group) for group in lone), [[1, 2, 5, 6], [3, 4]])
        self.assertTrue(
            all(
                score >= stories.JOINED_SCORE for group in lone for article, score in group.items() if article in (5, 6)
            )
        )
        big = [dict.fromkeys(range(1, 11), 0.5), dict.fromkeys(range(11, 21), 0.5)]
        self.assertEqual(stories._apply_joins(big, [(1, 11)], set()), big)  # would be larger than JOINED_MAX


class SimilarTests(unittest.TestCase):
    """似た記事 (D-66): other outlets' articles with a resembling English headline, not grouped."""

    def test_similar_needs_a_shared_name_and_skips_the_group(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "lib.sqlite3"
        rows = [
            (1, "BBC", "France's government under pressure after weekend of riots in Paris", "s1"),
            (2, "CNA", "Violent protests in France: curfew imposed in parts of Paris", None),
            (3, "Yonhap", "Curfew in parts of Paris as France is shocked by violence", "s1"),
            (4, "CNA", "Government under pressure after weekend of protests in Lima", None),
            (5, "BBC", "Paris riots: more arrests in France", None),
        ]
        with closing(index.connect(db)) as c:
            for aid, source, pivot, key in rows:
                c.execute(
                    "INSERT INTO articles(id, source, filename, title, published, size, mtime) VALUES(?,?,?,?,?,1,1)",
                    (aid, source, f"{aid}.html", pivot, "2026-10-19T10:00:00Z"),
                )
                c.execute(
                    "INSERT INTO story_items(article_id, title, lang, pivot, pivot_chars, pivot_at) VALUES(?,?,'en',?,0,'')",
                    (aid, pivot, pivot),
                )
                if key:
                    c.execute("INSERT INTO story_members(article_id, story_key, score) VALUES(?,?,0.5)", (aid, key))
        try:
            found = [item["id"] for item in stories.similar(db, 1)]
            self.assertIn(2, found)  # another outlet, France and Paris
            self.assertNotIn(3, found)  # already in the same group
            self.assertNotIn(4, found)  # common words only, no shared name
            self.assertNotIn(5, found)  # the same outlet
            self.assertEqual(stories.similar(db, 999), [])
            with closing(index.connect(db)) as c:
                c.execute("INSERT INTO story_exclusions(a, b, created) VALUES(1, 2, '')")
            self.assertNotIn(2, [item["id"] for item in stories.similar(db, 1)])  # the reader said: different news
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ArcTests(unittest.TestCase):
    """このニュースの流れ (D-74): groups of the same developing matter, oldest first."""

    AIRPORT = (
        "Houthi drones struck Riyadh airport terminal overnight, Saudi officials said, halting flights. "
        "The Houthi movement claimed the drone attack on the Riyadh airport terminal."
    )
    AIRPORT_ZH = "葉門青年運動以無人機攻擊沙烏地阿拉伯首都利雅德機場航廈，機場暫停航班。"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "articles"
        for site in ("BBC", "CNA"):
            (self.root / site).mkdir(parents=True)

        def page(title, published, text):
            return bbc(title, published).replace(f"Body of {title}.".encode(), text.encode())

        def cna_page(title, published, text):
            return cna(title, published).replace("本文。".encode(), text.encode())

        files = {
            ("BBC", "a1.html"): page("Riyadh airport hit by drones", "2026-10-11T08:00:00Z", self.AIRPORT),
            ("CNA", "z1.html"): cna_page("利雅德機場遭攻擊", "2026-10-11T09:00:00Z", self.AIRPORT_ZH),
            ("BBC", "a2.html"): page(
                "Second strike on Saudi capital's airport",
                "2026-10-12T08:00:00Z",
                self.AIRPORT + " A second Houthi drone attack hit the airport terminal again.",
            ),
            ("CNA", "z2.html"): cna_page(
                "利雅德機場再遭攻擊", "2026-10-13T09:00:00Z", self.AIRPORT_ZH + "機場航廈再度起火。"
            ),
            ("BBC", "o1.html"): page(
                "Snow closes Alpine passes",
                "2026-10-12T10:00:00Z",
                "Heavy snow closed mountain passes across the Alps, stranding skiers and lorries.",
            ),
            ("BBC", "o2.html"): page(
                "Avalanche warning in Alps",
                "2026-10-12T11:00:00Z",
                "Forecasters warned of avalanches after heavy snow in the Alps.",
            ),
        }
        for (site, name), data in files.items():
            (self.root / site / name).write_bytes(data)
        self.db = self.tmp / "library.sqlite3"
        index._last_sync.clear()
        index.sync(self.root, self.db, force=True)
        ids = {name: index.find_by_file(self.db, site, name)["id"] for site, name in files}
        groups = {"s1": ["a1.html", "z1.html"], "s2": ["a2.html"], "s3": ["z2.html"], "s9": ["o1.html", "o2.html"]}
        with closing(index.connect(self.db)) as c:
            for key, names in groups.items():
                for name in names:
                    c.execute(
                        "INSERT INTO story_members(article_id, story_key, score) VALUES(?,?,0.5)", (ids[name], key)
                    )
        stories._body_cache.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_follow_ups_in_one_language_by_their_text(self):
        arc = stories.arc(self.db, "s1")
        self.assertEqual([g["key"] for g in arc], ["s1", "s2", "s3"])  # oldest first
        self.assertEqual([g["current"] for g in arc], [True, False, False])
        self.assertEqual(arc[0]["count"], 2)
        self.assertEqual(stories.arc(self.db, "s9"), [])  # nothing else about snow in the Alps
        self.assertEqual(stories.arc(self.db, "nope"), [])

    def test_across_languages_needs_names_in_the_headlines(self):
        with closing(index.connect(self.db)) as c:
            rows = c.execute(
                "SELECT a.id, a.title FROM articles a JOIN story_members m ON m.article_id = a.id "
                "WHERE m.story_key IN ('s2', 's9')"
            ).fetchall()
            for row in rows:
                c.execute(
                    "INSERT INTO story_items(article_id, title, lang, pivot, pivot_chars, pivot_at) VALUES(?,?,'en',?,0,'')",
                    (row[0], row[1], row[1]),
                )
            # a Chinese-only group, its headline in English: shares Riyadh and Houthi with s2's
            c.execute(
                "INSERT INTO articles(id, source, filename, title, published, size, mtime) "
                "VALUES(900, 'CNA', 'x.html', '胡塞再襲利雅德', '2026-10-12T20:00:00Z', 1, 1)"
            )
            c.execute(
                "INSERT INTO story_items(article_id, title, lang, pivot, pivot_chars, pivot_at) VALUES(900, '胡塞再襲利雅德', 'zh', "
                "'Houthi drones strike Riyadh airport again', 0, '')"
            )
            c.execute("INSERT INTO story_members(article_id, story_key, score) VALUES(900, 's7', 0.5)")
        c2 = index.find_by_file(self.db, "BBC", "a2.html")["id"]
        with closing(index.connect(self.db)) as c:
            c.execute(
                "UPDATE story_items SET pivot = 'Houthi drones hit Riyadh airport a second time' WHERE article_id = ?",
                (c2,),
            )
        keys = [g["key"] for g in stories.arc(self.db, "s7")]
        self.assertIn("s2", keys)
        self.assertNotIn("s9", keys)

    def test_story_page_shows_the_arc(self):
        app = create_app(self.root, self.db, start_background=False, secret_key=b"k" * 32)
        html = app.test_client().get("/stories/s2", headers=H).get_data(as_text=True)
        self.assertIn("このニュースの流れ", html)
        self.assertIn('href="/stories/s1"', html)
        self.assertIn('aria-current="true"', html)
        html = app.test_client().get("/stories/s9", headers=H).get_data(as_text=True)
        self.assertNotIn("このニュースの流れ", html)

    def test_article_page_links_the_arc(self):
        app = create_app(self.root, self.db, start_background=False, secret_key=b"k" * 32)
        a1 = index.find_by_file(self.db, "BBC", "a1.html")["id"]
        html = app.test_client().get(f"/a/{a1}", headers=H).get_data(as_text=True)
        self.assertTrue('href="/stories/s1#arc-title"' in html and "続報あり" in html and "ほか 2 件" in html)
        o1 = index.find_by_file(self.db, "BBC", "o1.html")["id"]
        self.assertFalse("arc-title" in app.test_client().get(f"/a/{o1}", headers=H).get_data(as_text=True))

    def test_cached_until_the_groups_change(self):
        first = stories.arc(self.db, "s1")
        self.assertIs(stories.arc(self.db, "s1"), first)
        with closing(index.connect(self.db)) as c:
            c.execute("DELETE FROM story_members WHERE story_key = 's3'")
        self.assertEqual([g["key"] for g in stories.arc(self.db, "s1")], ["s1", "s2"])

    def test_digest_marks_a_follow_up(self):
        app = create_app(self.root, self.db, start_background=False, secret_key=b"k" * 32)
        z2 = index.find_by_file(self.db, "CNA", "z2.html")["id"]
        with closing(index.connect(self.db)) as c:  # s2 reported by two outlets (in the digest), all recent
            c.execute("UPDATE story_members SET story_key = 's2' WHERE article_id = ?", (z2,))
            for name, hours in (("a1.html", 40), ("z1.html", 39), ("a2.html", 10), ("z2.html", 9)):
                c.execute(
                    "UPDATE articles SET published = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) WHERE filename = ?",
                    (f"-{hours} hours", name),
                )
        html = app.test_client().get("/digest?hours=24", headers=H).get_data(as_text=True)
        self.assertTrue('class="arc-tag">続報' in html and 'href="/stories/s1"' in html)


class BodyApartTests(StoryTestCase):
    """D-75: same-language articles whose texts share almost nothing are not grouped."""

    def test_headlines_alike_texts_apart(self):
        from datetime import datetime

        from newsreader.library import stories_core as core

        t = datetime(2026, 10, 13, tzinfo=UTC)
        items = {1: (core.features(NOBEL_A), t), 2: (core.features(NOBEL_B), t)}
        self.assertEqual(len(core.cluster(items)), 1)
        self.assertEqual(core.cluster(items, apart=lambda a, b: (a, b) == (1, 2)), [])

    def test_refresh_uses_the_texts(self):
        def long_text(words):
            return " ".join(f"{w}{chr(97 + i)}{chr(97 + j)}" for i in range(6) for j in range(6) for w in words)

        self.write(
            "BBC",
            "a.html",
            bbc(NOBEL_A, "2026-10-13T10:00:00Z").replace(
                f"Body of {NOBEL_A}.".encode(), long_text(["alpha", "beta"]).encode()
            ),
        )
        self.write(
            "BBC",
            "b.html",
            bbc(NOBEL_B, "2026-10-13T12:00:00Z").replace(
                f"Body of {NOBEL_B}.".encode(), long_text(["gamma", "delta"]).encode()
            ),
        )
        index._last_sync.clear()
        index.sync(self.root, self.db, force=True)
        stories._body_cache.clear()
        self.refresh(FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN}))
        a, b = self.id_of("BBC", "a.html"), self.id_of("BBC", "b.html")
        self.assertFalse(any({a, b} <= members for members in self.groups().values()))


if __name__ == "__main__":
    unittest.main()
