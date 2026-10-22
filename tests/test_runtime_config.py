"""Per-PC library folder setting and the frozen static files of a production server."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from newsreader import paths
from newsreader.collect import runner
from newsreader.services import settings
from newsreader.web.assets import StaticFiles

H = {"Host": "localhost"}


class LibraryDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.library = self.tmp / "lib"
        self.library.mkdir()

    def tearDown(self):
        settings.SETTINGS_PATH.unlink(missing_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_order_env_then_setting_then_default(self):
        with mock.patch.dict("os.environ", {"HTML_DIRECTORY": ""}):
            self.assertEqual(paths.articles_dir(), paths.DEFAULT_ARTICLES_DIR)
            settings.save_library_dir(str(self.library))
            self.assertEqual(paths.articles_dir(), self.library)
        with mock.patch.dict("os.environ", {"HTML_DIRECTORY": str(self.tmp)}):
            self.assertEqual(paths.articles_dir(), self.tmp)
        # Older launchers pass the default folder explicitly: the saved folder still wins over it.
        with mock.patch.dict("os.environ", {"HTML_DIRECTORY": str(paths.DEFAULT_ARTICLES_DIR)}):
            self.assertEqual(paths.articles_dir(), self.library)
        # A launcher started before the folder was moved passes a folder that is gone: the saved one wins.
        with mock.patch.dict("os.environ", {"HTML_DIRECTORY": str(self.tmp / "moved-away")}):
            self.assertEqual(paths.articles_dir(), self.library)

    def test_launcher_uses_the_saved_folder(self):
        import launcher
        settings.save_library_dir(str(self.library))
        with mock.patch.object(launcher, "SETTINGS_PATH", settings.SETTINGS_PATH), \
                mock.patch.dict("os.environ", {"HTML_DIRECTORY": ""}):
            self.assertEqual(launcher.resolve_article_directory(), self.library.resolve())
        with mock.patch.dict("os.environ", {"HTML_DIRECTORY": str(self.tmp)}):
            self.assertEqual(launcher.resolve_article_directory(), self.tmp.resolve())

    def test_collector_uses_collector_json_then_setting(self):
        settings.save_library_dir(str(self.library))
        with mock.patch.object(runner, "COLLECTOR_SETTINGS", self.tmp / "missing.json"):
            self.assertEqual(runner.get_library_root(), self.library.resolve())
        explicit = self.tmp / "collector.json"
        explicit.write_text(json.dumps({"library_root": str(self.tmp)}), encoding="utf-8")
        with mock.patch.object(runner, "COLLECTOR_SETTINGS", explicit):
            self.assertEqual(runner.get_library_root(), self.tmp.resolve())

    def test_validation(self):
        self.assertEqual(settings.validate_library_dir("  "), "")
        with self.assertRaises(ValueError):
            settings.validate_library_dir("relative/dir")
        with self.assertRaises(ValueError):
            settings.validate_library_dir(str(self.tmp / "nope"))
        link = self.tmp / "link"
        try:
            link.symlink_to(self.library, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        with self.assertRaises(ValueError):
            settings.validate_library_dir(str(link))

    def test_settings_page_saves_and_keeps_other_keys(self):
        from newsreader.web import create_app
        app = create_app(self.library, self.tmp / "db.sqlite3", start_background=False, secret_key=b"k" * 32)
        client = app.test_client()
        page = client.get("/translation-settings", headers=H).get_data(as_text=True)
        self.assertIn(str(self.library), page)
        with client.session_transaction() as session:
            token = session["csrf"]
        form = {"csrf": token, "provider": "azure", "deepl_endpoint": "auto", "azure_region": "",
                "network_bind_host": "", "network_port": "", "naver_search_query": "뉴스",
                "library_dir": str(self.library)}
        self.assertIn("設定を保存しました", client.post("/translation-settings", headers=H, data=form).get_data(as_text=True))
        self.assertEqual(paths.configured_library_dir(), str(self.library))
        bad = client.post("/translation-settings", headers=H, data={**form, "library_dir": "nowhere"}).get_data(as_text=True)
        self.assertIn("絶対パス", bad)
        self.assertEqual(paths.configured_library_dir(), str(self.library))
        self.assertIn(str(self.library), client.get("/status", headers=H).get_data(as_text=True))


class FrozenStaticTests(unittest.TestCase):
    def test_production_server_keeps_serving_what_it_started_with(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "app.js").write_text("old", encoding="utf-8")
            frozen, live = StaticFiles(tmp, frozen=True), StaticFiles(tmp)
            self.assertEqual(frozen.get("app.js").data, b"old")
            (tmp / "app.js").write_text("new version", encoding="utf-8")
            (tmp / "added.js").write_text("x", encoding="utf-8")
            self.assertEqual(frozen.get("app.js").data, b"old")   # matches the templates it compiled
            self.assertIsNone(frozen.get("added.js"))
            self.assertEqual(live.get("app.js").data, b"new version")
            self.assertIsNone(frozen.get("../etc/passwd"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
