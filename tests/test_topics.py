"""話題を追う (D-61): follow a search; the home page shows its newest matches in any language."""

import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.library import index
from newsreader.services import cache, settings
from newsreader.services import translation as ts
from newsreader.web import create_app

H = {"Host": "localhost"}


def iso(hours_ago):
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TopicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "articles").mkdir()
        self.db = self.tmp / "library.sqlite3"
        rows = [
            (1, "BBC", "Taiwan holds drills near strait", "en", "Taiwan holds drills near strait", 2),
            (2, "CNA", "台灣舉行軍演", "zh", "Taiwan holds military exercises", 5),
            (3, "Yonhap", "대만 해협 긴장", "ko", "Tension in the Taiwan Strait", 50),
            (4, "BBC", "Football results", "en", "Football results", 1),
        ]
        with closing(index.connect(self.db)) as db:
            for aid, source, title, lang, pivot, hours in rows:
                db.execute(
                    "INSERT INTO articles(id, source, filename, title, published, paragraph_count, size, mtime)"
                    " VALUES(?,?,?,?,?,5,1,1)",
                    (aid, source, f"{aid}.html", title, iso(hours)),
                )
                db.execute(
                    "INSERT INTO story_items(article_id, title, lang, pivot, pivot_chars, pivot_at) VALUES(?,?,?,?,0,'')",
                    (aid, title, lang, pivot),
                )
        self.patches = [
            mock.patch.object(settings, "SETTINGS_PATH", self.tmp / "settings.json"),
            mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3"),
            mock.patch.object(ts, "active_provider", lambda: "azure"),
            mock.patch.object(ts, "provider_ready", lambda provider=None: True),
            mock.patch.object(
                ts, "_translate_batch", lambda provider, texts, target: ["Taiwan" if t == "台湾" else t for t in texts]
            ),
        ]
        for patch in self.patches:
            patch.start()
        self.client = create_app(
            self.tmp / "articles", self.db, start_background=False, secret_key=b"t" * 32
        ).test_client()
        self.client.get("/", headers=H)
        with self.client.session_transaction() as session:
            self.csrf = session["csrf"]

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def follow(self, topic, follow=True):
        return self.client.post(
            "/api/topics", json={"topic": topic, "follow": follow}, headers={**H, "X-CSRF-Token": self.csrf}
        )

    def test_saving_topics(self):
        self.assertEqual(settings.save_topic("  Taiwan  ", True), ["Taiwan"])
        self.assertEqual(settings.save_topic("台湾", True), ["台湾", "Taiwan"])
        self.assertEqual(settings.save_topic("taiwan", True), ["taiwan", "台湾"])  # the same topic, moved up
        self.assertEqual(settings.save_topic("TAIWAN", False), ["台湾"])
        for n in range(settings.MAX_TOPICS - 1):
            settings.save_topic(f"topic {n}", True)
        with self.assertRaises(ValueError):
            settings.save_topic("one too many", True)
        with self.assertRaises(ValueError):
            settings.save_topic("x" * 61, True)

    def test_home_lists_matches_in_every_language(self):
        self.assertEqual(self.follow("台湾").get_json(), {"topics": ["台湾"]})
        home = self.client.get("/", headers=H).get_data(as_text=True)
        self.assertIn("追っている話題", home)
        card = home[home.index('data-topic-card="台湾"') : home.index("本すべて")]
        self.assertIn("Taiwan holds drills", card)
        self.assertIn("台灣舉行軍演", card)  # Chinese headline, found through its English pivot
        self.assertIn("24時間で 2 本", card)  # the Korean one is older than a day
        self.assertNotIn("Football", card)
        self.assertIn('lang="zh-Hant"', card)

    def test_search_page_offers_to_follow_and_stop(self):
        page = self.client.get("/?q=Taiwan", headers=H).get_data(as_text=True)
        self.assertIn('data-topic-follow="Taiwan" aria-pressed="false"', page)
        self.follow("Taiwan")
        page = self.client.get("/?q=taiwan", headers=H).get_data(as_text=True)
        self.assertIn('aria-pressed="true">追跡中（やめる）', page)
        self.follow("Taiwan", False)
        self.assertNotIn("追っている話題", self.client.get("/", headers=H).get_data(as_text=True))

    def test_outlet_chips_count_the_matches(self):
        page = self.client.get("/?q=Taiwan", headers=H).get_data(as_text=True)
        self.assertIn("BBC<span>1</span>", page)  # not the football article
        self.assertIn("CNA<span>1</span>", page)  # through its English headline
        self.assertIn("BBC<span>2</span>", self.client.get("/", headers=H).get_data(as_text=True))

    def test_settings_lists_and_removes_topics(self):
        self.follow("Taiwan")
        self.follow("台湾")
        page = self.client.get("/translation-settings", headers=H).get_data(as_text=True)
        self.assertIn('value="remove_topic:1"', page)
        self.client.post("/translation-settings", headers=H, data={"csrf": self.csrf, "action": "remove_topic:1"})
        self.assertEqual(settings.topics(), ["台湾"])

    def test_bad_requests(self):
        self.assertEqual(self.follow("", True).status_code, 400)
        self.assertEqual(
            self.client.post(
                "/api/topics", json={"topic": "a", "follow": "yes"}, headers={**H, "X-CSRF-Token": self.csrf}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/api/topics", json={"topic": "a", "follow": True}, headers=H).status_code, 400
        )


if __name__ == "__main__":
    unittest.main()
