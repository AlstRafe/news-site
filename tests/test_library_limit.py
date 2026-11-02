"""A library limit (test builds, D-53): the collector stops adding articles once the library holds that many."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.collect import autofetch
from newsreader.services import settings

SOURCES = [{"name": "A", "directory": "A"}, {"name": "B", "directory": "B"}]


class LibraryLimitTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fetched = []

        def fetch(source, link, directory):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{link.rsplit('/', 1)[1]}.html").write_text("<html></html>", encoding="utf-8")
            self.fetched.append(link)
            return link

        self.patches = [
            mock.patch.object(autofetch, "SOURCES", SOURCES),
            mock.patch.object(
                autofetch,
                "_article_links",
                lambda source, feed="": [f"https://x/{source['name']}{i}" for i in range(5)],
            ),
            mock.patch.object(autofetch, "_fetch_article", fetch),
            mock.patch.object(autofetch, "_existing_article_urls", lambda directory, keep=(): set()),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_without_a_limit_each_source_adds_its_usual_few(self):
        totals = autofetch.fetch_cycle(self.root)
        self.assertEqual(
            totals, {"A": autofetch.MAX_NEW_PER_SOURCE_PER_CYCLE, "B": autofetch.MAX_NEW_PER_SOURCE_PER_CYCLE}
        )

    def test_collection_stops_at_the_limit_and_says_why(self):
        (self.root / "Old").mkdir()
        (self.root / "Old" / "kept.html").write_text("x", encoding="utf-8")
        report = {}
        totals = autofetch.fetch_cycle(self.root, report=report, limit=3)
        self.assertEqual(totals, {"A": 2, "B": 0})
        self.assertEqual(len(list(self.root.rglob("*.html"))), 3)
        self.assertIn("上限（3 本）", report["B"]["error"])
        self.assertTrue((self.root / "Old" / "kept.html").exists())  # nothing is ever removed

    def test_header_badge_explains_the_limit(self):
        from newsreader.library import status

        report = {}
        (self.root / "A").mkdir()
        for i in range(3):
            (self.root / "A" / f"{i}.html").write_text("x", encoding="utf-8")
        autofetch.fetch_cycle(self.root, report=report, limit=3)
        badge = status.badge({"collector": {"finished": status.now_iso(), "ok": True, "sites": report}})
        self.assertEqual(badge["label"], "記事数の上限")
        sample = status.badge({"collector": {}}, collects=False)
        self.assertEqual(sample["label"], "サンプル記事")
        self.assertEqual(status.badge({"collector": {}})["label"], "未取得")
        normal = status.badge({"collector": {"finished": status.now_iso(), "ok": True, "sites": {"A": {"saved": 2}}}})
        self.assertTrue(normal["label"].startswith("取得"))

    def test_limit_setting(self):
        path = self.root / "settings.json"
        with mock.patch.object(settings, "SETTINGS_PATH", path):
            self.assertEqual(settings.library_limit(), 0)
            for value, expected in ((5, 5), (0, 0), (-1, 0), ("5", 0), (True, 0)):
                path.write_text(json.dumps({"library_limit": value}), encoding="utf-8")
                self.assertEqual(settings.library_limit(), expected, value)


if __name__ == "__main__":
    unittest.main()
