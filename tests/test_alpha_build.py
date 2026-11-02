"""Test build (D-53): one ZIP with the same code, a preset, sample articles and a relay token."""

import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launcher
import make_alpha_zip
import make_portable_zip
from newsreader import paths
from newsreader.library import index
from newsreader.services import preset, settings


class ChooseArticlesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "library.sqlite3"
        with closing(index.connect(self.db)) as db:
            rows = [  # id, source, published, paragraphs, story
                (1, "BBC", "2026-10-19T01", 9, "s1"),
                (2, "CNA", "2026-10-19T02", 9, "s1"),
                (3, "Yonhap", "2026-10-19T03", 9, "s1"),
                (4, "BBC", "2026-10-19T04", 9, "s1"),
                (5, "BBC", "2026-10-19T09", 9, "s2"),
                (6, "CNA", "2026-10-19T09", 9, "s2"),
                (7, "BBC", "2026-10-19T08", 9, None),
                (8, "Yonhap", "2026-10-19T07", 1, None),
                (9, "Yonhap", "2026-10-19T06", 9, None),
                (10, "CNA", "2026-10-19T10", 9, None),
            ]
            for aid, source, published, paragraphs, story in rows:
                db.execute(
                    "INSERT INTO articles(id, source, filename, title, published, paragraph_count, size, mtime)"
                    " VALUES(?,?,?,?,?,?,1,1)",
                    (aid, source, f"{aid}.html", f"t{aid}", published, paragraphs),
                )
                if story:
                    db.execute(
                        "INSERT INTO story_members(article_id, story_key, score) VALUES(?,?,?)", (aid, story, 1.0 / aid)
                    )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_story_from_every_outlet_first_then_the_newest(self):
        chosen = make_alpha_zip.choose_articles(self.db, 5)
        ids = [c["id"] for c in chosen]
        self.assertEqual(sorted(ids[:3]), [1, 2, 3])  # the story three outlets reported, one article each
        self.assertEqual(len(ids), 5)
        self.assertNotIn(8, ids)  # too short to read
        self.assertEqual(ids[3:], [10, 6])  # then simply the newest (ties: higher id)

    def test_a_clearly_matched_group_is_preferred(self):
        with closing(index.connect(self.db)) as db:
            db.execute("UPDATE story_members SET score = 0.5 WHERE story_key = 's2'")
        chosen = make_alpha_zip.choose_articles(self.db, 5)
        self.assertEqual([c["id"] for c in chosen][:2], [5, 6])  # s2: two outlets, all members close

    def test_a_group_with_a_figure_disagreement_shows_off_best(self):
        with closing(index.connect(self.db)) as db:
            db.execute("UPDATE story_members SET score = 0.5")
            db.execute("DELETE FROM story_members WHERE story_key = 's1' AND article_id IN (1, 4)")  # s1: CNA + Yonhap
            db.execute("INSERT INTO story_facts(story_key, members, differs) VALUES('s1', '2,3', 1)")
        chosen = [c["id"] for c in make_alpha_zip.choose_articles(self.db, 5)]
        self.assertEqual(sorted(chosen[:2]), [2, 3])  # s1, not the newer s2

    def test_the_groups_update_comes_along(self):
        with closing(index.connect(self.db)) as db:
            db.execute("UPDATE story_members SET score = 0.5")
            db.execute("DELETE FROM story_members WHERE story_key = 's1' AND article_id = 3")  # s1: BBC x2 + CNA
            db.execute("DELETE FROM story_members WHERE story_key = 's2'")
        chosen = [c["id"] for c in make_alpha_zip.choose_articles(self.db, 5)]
        self.assertEqual(sorted(chosen[:3]), [1, 2, 4])  # both BBC reports of the news, and CNA's

    def test_only_outlets_still_collected(self):
        chosen = make_alpha_zip.choose_articles(self.db, 5, {"BBC", "Yonhap"})
        self.assertEqual({c["source"] for c in chosen}, {"BBC", "Yonhap"})
        self.assertEqual([c["id"] for c in chosen][:2], [1, 3])

    def test_the_index_is_only_read(self):
        before = self.db.read_bytes()
        make_alpha_zip.choose_articles(self.db, 3)
        self.assertEqual(self.db.read_bytes(), before)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.article = self.tmp / "a.html"
        self.article.write_text("<html>article</html>", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zip_has_the_same_code_as_the_portable_zip_plus_the_preset(self):
        archive = make_alpha_zip.build(
            self.tmp, [(self.article, "BBC/a.html")], "http://100.64.0.1:5000", "nbr_token", 5, "20261019-2100"
        )
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
            preset_settings = json.loads(z.read("NewsBridge-alpha/preset/settings.json"))
            self.assertEqual(z.read("NewsBridge-alpha/preset/relay_token"), b"nbr_token")
            self.assertEqual(z.read("NewsBridge-alpha/launcher.py"), (ROOT / "launcher.py").read_bytes())
        code = {
            f"NewsBridge-alpha/{p.relative_to(ROOT).as_posix()}"
            for p in ROOT.rglob("*")
            if p.is_file() and make_portable_zip.should_include(p, p.relative_to(ROOT))
        }
        extra = {
            "NewsBridge-alpha/articles/BBC/a.html",
            "NewsBridge-alpha/preset/settings.json",
            "NewsBridge-alpha/preset/relay_token",
            "NewsBridge-alpha/はじめにお読みください.txt",
        }
        self.assertEqual(names, code | extra)
        self.assertFalse(any("/instance/" in n or "/tests/" in n or n.endswith((".sqlite3", ".pyc")) for n in names))
        self.assertEqual(preset_settings["translation_source"], "relay")
        self.assertEqual(preset_settings["network_bind_host"], "127.0.0.1")
        self.assertEqual(preset_settings["library_limit"], 5)
        self.assertFalse(preset_settings["start_at_logon"])
        self.assertFalse(preset_settings["collect_hourly"])
        self.assertTrue(set(preset_settings) - {"preset_id"} <= launcher.PRESET_KEYS)

    def test_without_a_token_the_zip_carries_none_and_says_where_to_enter_it(self):
        archive = make_alpha_zip.build(
            self.tmp, [(self.article, "BBC/a.html")], "http://100.64.0.1:5000", "", 5, "20261023-2200"
        )
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
            readme = z.read("NewsBridge-alpha/はじめにお読みください.txt").decode("utf-8")
        self.assertNotIn("NewsBridge-alpha/preset/relay_token", names)
        self.assertFalse(any("nbr_" in n for n in names))
        self.assertIn("別に受け取った「合言葉」", readme)
        self.assertNotIn("この ZIP には翻訳の中継用の合言葉が入っています", readme)

    def test_the_relay_url_prefers_the_tailscale_address(self):
        (self.tmp / "instance").mkdir()
        (self.tmp / "instance" / "status.json").write_text(
            json.dumps({"web": {"url": "http://192.168.1.5:5000/", "also": ["http://100.70.63.91:5000/"]}}),
            encoding="utf-8",
        )
        with mock.patch.object(make_alpha_zip, "PROJECT_ROOT", self.tmp):
            self.assertEqual(make_alpha_zip.default_relay_url(), "http://100.70.63.91:5000")

    def test_the_glossary_travels_with_the_test_build(self):
        glossary = [{"source": "다카이치", "target": "高市", "lang": "ja"}]
        archive = make_alpha_zip.build(
            self.tmp,
            [(self.article, "BBC/a.html")],
            "http://100.64.0.1:5000",
            "nbr_token",
            5,
            "20261020-0900",
            glossary=glossary,
        )
        with zipfile.ZipFile(archive) as z:
            preset_settings = json.loads(z.read("NewsBridge-alpha/preset/settings.json"))
        self.assertEqual(preset_settings["glossary"], glossary)
        self.assertIn("glossary", launcher.PRESET_KEYS)

    def test_portable_zip_never_ships_a_preset(self):
        self.assertFalse(make_portable_zip.should_include(ROOT / "preset" / "relay_token", Path("preset/relay_token")))


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(launcher, "PRESET_SETTINGS_PATH", self.tmp / "preset" / "settings.json"),
            mock.patch.object(launcher, "PRESET_RECORD_PATH", self.tmp / "instance" / "preset-applied.json"),
            mock.patch.object(launcher, "SETTINGS_PATH", self.tmp / "instance" / "settings.json"),
        ]
        for patch in self.patches:
            patch.start()
        (self.tmp / "preset").mkdir()
        (self.tmp / "instance").mkdir()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_preset(self, **values):
        (self.tmp / "preset" / "settings.json").write_text(json.dumps(values), encoding="utf-8")

    def test_no_preset_is_an_ordinary_installation(self):
        self.assertFalse(launcher.apply_preset())
        self.assertEqual(launcher.read_settings(), {})

    def test_preset_is_applied_once_and_later_changes_stay(self):
        self.write_preset(
            preset_id="p1",
            translation_source="relay",
            relay_url="http://100.64.0.1:5000",
            network_bind_host="127.0.0.1",
            library_limit=5,
            provider="evil",
            azure_region="x",
        )
        self.assertTrue(launcher.apply_preset())
        saved = launcher.read_settings()
        self.assertEqual((saved["translation_source"], saved["library_limit"]), ("relay", 5))
        self.assertNotIn("provider", saved)  # only the keys a preset may set
        launcher.write_settings(dict(saved, library_limit=9))
        self.assertTrue(launcher.apply_preset())
        self.assertEqual(launcher.read_settings()["library_limit"], 9)

    def test_a_preset_cannot_listen_everywhere(self):
        self.write_preset(preset_id="p2", network_bind_host="0.0.0.0")
        with self.assertRaises(ValueError):
            launcher.apply_preset()

    def run_main(self):
        with (
            mock.patch.dict(launcher.os.environ, {"LOCALAPPDATA": str(self.tmp / "local")}),
            mock.patch.object(launcher, "select_connection", return_value=("127.0.0.1", 5000, {"127.0.0.1"})),
            mock.patch.object(launcher, "find_runtime_python", return_value=Path("python")),
            mock.patch.object(launcher, "update_dependencies"),
            mock.patch.object(launcher, "persist_collector_config"),
            mock.patch.object(launcher, "ensure_logon_task") as logon,
            mock.patch.object(launcher, "ensure_hourly_collector_task") as hourly,
            mock.patch.object(launcher, "launch_app", return_value=0),
            mock.patch("builtins.input", side_effect=AssertionError("asked")),
        ):
            self.assertEqual(launcher.main([]), 0)
        return hourly, logon

    def test_a_test_build_registers_no_windows_tasks(self):
        self.write_preset(preset_id="p3", collect_hourly=False, start_at_logon=False)
        hourly, logon = self.run_main()
        hourly.assert_not_called()
        self.assertFalse(logon.call_args.args[1])

    def test_an_ordinary_copy_still_registers_the_hourly_task(self):
        (self.tmp / "local" / launcher.APP_CONFIG["name"]).mkdir(parents=True)
        (self.tmp / "local" / launcher.APP_CONFIG["name"] / "first-run-complete").write_text("x")
        hourly, logon = self.run_main()
        hourly.assert_called_once()
        self.assertTrue(logon.call_args.args[1])

    def test_first_run_asks_nothing_in_a_preset_build(self):
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            self.assertFalse(launcher.prepare_first_run({}, self.tmp / "marker", interactive=False))


class TokenImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.token = self.tmp / "relay_token"
        self.patch = mock.patch.object(paths, "PRESET_TOKEN_PATH", self.token)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        settings.delete_secret("relay_token")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shipped_token_moves_into_the_credential_store(self):
        self.assertFalse(preset.import_relay_token())
        self.token.write_text("nbr_abc\n", encoding="utf-8")
        self.assertTrue(preset.import_relay_token())
        self.assertEqual(settings.get_secret("relay_token"), "nbr_abc")
        self.assertFalse(self.token.exists())

    def test_token_stays_when_the_store_keeps_nothing(self):
        self.token.write_text("nbr_abc", encoding="utf-8")
        with mock.patch.object(preset, "set_secret"), mock.patch.object(preset, "get_secret", return_value=None):
            self.assertFalse(preset.import_relay_token())
        self.assertTrue(self.token.exists())

    def test_a_token_left_in_preset_is_still_used(self):
        from newsreader.services import relay

        self.token.write_text("nbr_left", encoding="utf-8")
        with mock.patch.object(relay, "get_secret", side_effect=settings.SecretStoreError("x")):
            self.assertEqual(relay._token(), "nbr_left")

    def test_token_stays_when_the_store_fails(self):
        self.token.write_text("nbr_abc", encoding="utf-8")
        with mock.patch.object(preset, "set_secret", side_effect=settings.SecretStoreError("x")):
            self.assertFalse(preset.import_relay_token())
        self.assertTrue(self.token.exists())


if __name__ == "__main__":
    unittest.main()
