"""媒体くらべ (D-62): the outlets compared from the library alone."""

import json
import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.library import index, outlets
from newsreader.web import create_app

NOW = datetime(2026, 10, 20, 12, 0, 0)


def at(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class OutletTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "articles").mkdir()
        self.db = self.tmp / "library.sqlite3"
        rows = [  # id, source, hours ago, story
            (1, "BBC", 10, "s1"),
            (2, "CNA", 8, "s1"),
            (3, "Yonhap", 7, "s1"),  # BBC first; CNA +2h, Yonhap +3h
            (4, "CNA", 5, "s4"),
            (5, "BBC", 4, "s4"),  # CNA first; BBC +1h
            (6, "BBC", 3, "s6"),
            (7, "BBC", 2, "s6"),  # BBC only: two scoop articles
            (8, "Yonhap", 1, None),  # never grouped: a scoop
            (9, "CNA", 24 * 9, "s9"),
            (10, "BBC", 24 * 9, "s9"),  # older than a week
        ]
        with closing(index.connect(self.db)) as db:
            for aid, source, hours, key in rows:
                db.execute(
                    "INSERT INTO articles(id, source, filename, title, published, size, mtime) VALUES(?,?,?,?,?,1,1)",
                    (aid, source, f"{aid}.html", f"t{aid}", at(hours)),
                )
                if key:
                    db.execute("INSERT INTO story_members(article_id, story_key, score) VALUES(?,?,0.5)", (aid, key))
            summary = [
                {
                    "kind": "dead",
                    "label": "死者",
                    "differs": True,
                    "trail": [{"source": "Yonhap", "text": "20人"}, {"source": "BBC", "text": "50人"}],
                }
            ]
            db.execute(
                "INSERT INTO story_facts(story_key, members, newest, differs, cohesive, summary) VALUES('s1','1,2,3',?,1,1,?)",
                (at(7), json.dumps(summary)),
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_per_outlet_figures(self):
        found = outlets.stats(self.db, days=7, now=NOW)
        by = {o["source"]: o for o in found["outlets"]}
        self.assertEqual([o["source"] for o in found["outlets"]], ["BBC", "CNA", "Yonhap"])  # most articles first
        self.assertEqual((by["BBC"]["articles"], by["BBC"]["shared"], by["BBC"]["scoops"]), (4, 2, 2))
        self.assertEqual((by["BBC"]["first"], by["BBC"]["races"], by["BBC"]["median_lag_hours"]), (1, 2, 1.0))
        self.assertEqual((by["CNA"]["first"], by["CNA"]["median_lag_hours"]), (1, 2.0))
        self.assertEqual((by["Yonhap"]["scoops"], by["Yonhap"]["median_lag_hours"]), (1, 3.0))
        self.assertEqual((by["BBC"]["disputes"], by["Yonhap"]["disputes"], by["CNA"]["disputes"]), (1, 1, 0))
        self.assertEqual(found["shared_news"], 2)
        self.assertEqual([a["id"] for a in by["BBC"]["recent_scoops"]], [7, 6])  # newest first, only BBC's own news
        self.assertEqual([a["id"] for a in by["Yonhap"]["recent_scoops"]], [8])
        self.assertEqual(
            {(p["a"], p["b"]): p["news"] for p in found["pairs"]},
            {("BBC", "CNA"): 2, ("BBC", "Yonhap"): 1, ("CNA", "Yonhap"): 1},
        )

    def test_page(self):
        client = create_app(self.tmp / "articles", self.db, start_background=False, secret_key=b"o" * 32).test_client()
        from unittest import mock

        real = outlets.stats
        with mock.patch.object(outlets, "stats", lambda db, days=7: real(db, days=days, now=NOW)):
            page = client.get("/outlets?days=30", headers={"Host": "localhost"}).get_data(as_text=True)
        self.assertIn("媒体くらべ", page)
        self.assertIn("一緒に報じたニュース", page.replace("<wbr>", ""))
        self.assertEqual(client.get("/outlets?days=abc", headers={"Host": "localhost"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
