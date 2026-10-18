"""The hourly collection end to end, offline: discovery -> robots.txt -> fetch -> save ->
status.json -> index -> visible on the list page with a fresh header badge.

The news sites are replaced by canned responses; everything is written to a temp dir.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from newsreader.collect import autofetch, runner
from newsreader.library import index, status
from newsreader.services import settings
from newsreader.web import create_app
from test_extractor import CNA, NORMAL

ROBOTS_OK = b"User-agent: *\nAllow: /\n"
BBC_ARTICLE = NORMAL.replace(b"<head>", b'<head><link rel="canonical" href="https://www.bbc.com/news/articles/c1">')
BBC_NO_BODY = b'<html><head><link rel="canonical" href="https://www.bbc.com/news/videos/v1"></head><body></body></html>'
CNA_ARTICLE = CNA.replace(b"https://www.cna.com.tw/news/aopl/example.aspx", b"https://www.cna.com.tw/news/aopl/202610180001.aspx")

BBC_FEED = b"""<?xml version="1.0"?><rss><channel>
<item><link>https://www.bbc.com/news/articles/c1</link></item>
<item><link>https://www.bbc.com/news/videos/v1</link></item>
<item><link>https://www.bbc.com/sport/ignored</link></item>
</channel></rss>"""
CNA_FEED = b"""<?xml version="1.0"?><rss><channel>
<item><link>https://www.cna.com.tw/news/aopl/202610180001.aspx</link></item>
</channel></rss>"""

RESPONSES = {
    "https://feeds.bbci.co.uk/robots.txt": (ROBOTS_OK, "text/plain"),
    "https://www.bbc.com/robots.txt": (ROBOTS_OK, "text/plain"),
    "https://feeds.feedburner.com/robots.txt": (ROBOTS_OK, "text/plain"),
    "https://www.cna.com.tw/robots.txt": (ROBOTS_OK, "text/plain"),
    "https://feeds.bbci.co.uk/news/world/rss.xml": (BBC_FEED, "application/rss+xml"),
    "https://feeds.feedburner.com/rsscna/intworld": (CNA_FEED, "application/rss+xml"),
    "https://www.bbc.com/news/articles/c1": (BBC_ARTICLE, "text/html"),
    "https://www.bbc.com/news/videos/v1": (BBC_NO_BODY, "text/html"),
    "https://www.cna.com.tw/news/aopl/202610180001.aspx": (CNA_ARTICLE, "text/html"),
}


class FakeResponse(io.BytesIO):
    def __init__(self, url, data, content_type):
        super().__init__(data)
        self.url = url
        self.headers = mock.Mock()
        self.headers.get_content_type.return_value = content_type

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(request, timeout=None):
    url = request.full_url if hasattr(request, "full_url") else request
    if url not in RESPONSES:
        from urllib.error import HTTPError
        raise HTTPError(url, 404, "not found", {}, None)
    data, content_type = RESPONSES[url]
    return FakeResponse(url, data, content_type)


class CollectionEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.instance = self.tmp / "instance"
        self.root = self.tmp / "articles"
        self.root.mkdir()
        autofetch._robots_cache.clear()
        self.patches = [
            mock.patch.object(autofetch, "urlopen", fake_urlopen),
            mock.patch.object(runner, "INSTANCE_DIRECTORY", self.instance),
            mock.patch.object(runner, "LOCK_FILE", self.instance / "collector.lock"),
            mock.patch.object(runner, "get_library_root", lambda: self.root),
            mock.patch.object(runner.library, "DEFAULT_DB_PATH", self.instance / "library.sqlite3"),
            mock.patch.object(runner, "get_secret", lambda name: None),
            mock.patch.object(settings, "SETTINGS_PATH", self.instance / "settings.json"),
            mock.patch.object(sys, "argv", ["collector.py", "--once"]),
        ]
        for patch in self.patches:
            patch.start()
        # Logging must go to the temp instance dir, not the real one.
        for handler in list(autofetch._log.handlers):
            autofetch._log.removeHandler(handler)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        for handler in list(autofetch._log.handlers):
            handler.close()
            autofetch._log.removeHandler(handler)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_collection_saves_reports_indexes_and_shows_on_the_list(self):
        self.assertEqual(runner.main(), 0)
        saved = sorted(p.relative_to(self.root).as_posix() for p in self.root.glob("*/*.html"))
        self.assertEqual(len(saved), 2, saved)  # BBC text article + CNA article; the video page has no body

        data = json.loads((self.instance / "status.json").read_text(encoding="utf-8"))
        collector = data["collector"]
        self.assertTrue(collector["ok"])
        self.assertFalse(collector["running"])
        self.assertEqual(collector["sites"]["BBC"]["saved"], 1)
        self.assertEqual(collector["sites"]["BBC"]["skipped"], 1)
        self.assertEqual(collector["sites"]["CNA"]["saved"], 1)
        self.assertEqual(collector["sites"]["NAVER NEWS"]["error"], "APIキー未設定")
        self.assertFalse(collector["naver_configured"])
        self.assertEqual(data["library"]["result"]["added"], 2)

        app = create_app(self.root, self.instance / "library.sqlite3", start_background=False, secret_key=b"k" * 32)
        page = app.test_client().get("/", headers={"Host": "localhost"}).get_data(as_text=True)
        self.assertIn("News Headline", page)
        self.assertIn("CNA headline", page)
        self.assertIn("取得 たった今", page)
        status_page = app.test_client().get("/status", headers={"Host": "localhost"}).get_data(as_text=True)
        self.assertIn("APIキー未設定", status_page)

        # Second run: everything is a duplicate, nothing new saved, still ok.
        autofetch._robots_cache.clear()
        self.assertEqual(runner.main(), 0)
        self.assertEqual(len(list(self.root.glob("*/*.html"))), 2)
        again = json.loads((self.instance / "status.json").read_text(encoding="utf-8"))["collector"]
        self.assertEqual(again["sites"]["BBC"]["duplicates"] + again["sites"]["BBC"]["saved"], 1)

    def test_unreachable_site_is_reported_and_others_still_collected(self):
        broken = dict(RESPONSES)
        del broken["https://feeds.bbci.co.uk/news/world/rss.xml"]

        def partial(request, timeout=None):
            url = request.full_url
            if url == "https://feeds.bbci.co.uk/news/world/rss.xml":
                from urllib.error import URLError
                raise URLError("offline")
            return fake_urlopen(request, timeout)

        with mock.patch.object(autofetch, "urlopen", partial):
            self.assertEqual(runner.main(), 0)
        collector = json.loads((self.instance / "status.json").read_text(encoding="utf-8"))["collector"]
        self.assertIn("失敗", collector["sites"]["BBC"]["error"])
        self.assertEqual(collector["sites"]["CNA"]["saved"], 1)

    def test_credential_store_failure_skips_only_naver(self):
        from newsreader.services.settings import SecretStoreError

        def broken(name):
            raise SecretStoreError("vault locked")

        with mock.patch.object(runner, "get_secret", broken):
            self.assertEqual(runner.main(), 0)
        collector = json.loads((self.instance / "status.json").read_text(encoding="utf-8"))["collector"]
        self.assertTrue(collector["ok"])
        self.assertEqual(collector["sites"]["CNA"]["saved"], 1)

    def test_crash_is_recorded_as_a_failed_collection(self):
        with mock.patch.object(runner, "fetch_cycle", side_effect=RuntimeError("boom")):
            self.assertEqual(runner.main(), 1)
        collector = json.loads((self.instance / "status.json").read_text(encoding="utf-8"))["collector"]
        self.assertFalse(collector["ok"])
        self.assertIn("RuntimeError", collector["error"])
        self.assertEqual(status.badge({"collector": collector})["tone"], "bad")


class StatusFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "status.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_and_corrupt_files_never_break_reading(self):
        self.assertEqual(status.read(self.path), {})
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(status.read(self.path), {})
        self.assertEqual(status.badge({})["tone"], "idle")

    def test_corrupt_file_after_a_good_one_keeps_the_last_good_view(self):
        status.update("collector", {"ok": True, "finished": status.now_iso()}, path=self.path)
        good = status.read(self.path)
        self.path.write_text('{"collector": ', encoding="utf-8")  # a torn write
        os.utime(self.path, ns=(1, 1))
        self.assertEqual(status.read(self.path), good)

    def test_update_merges_sections_and_leaves_no_temp_files(self):
        status.update("collector", {"ok": True}, path=self.path)
        status.update("library", {"synced": "x"}, path=self.path)
        status.update("collector", {"seconds": 3}, path=self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"collector": {"ok": True, "seconds": 3}, "library": {"synced": "x"}})
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["status.json"])

    def test_pages_render_with_a_corrupt_status_file(self):
        from newsreader.web import create_app
        root = self.tmp / "articles"
        root.mkdir()
        self.path.write_text("garbage", encoding="utf-8")
        client = create_app(root, self.tmp / "library.sqlite3", start_background=False, secret_key=b"k" * 32).test_client()
        for url in ("/", "/status"):
            self.assertEqual(client.get(url, headers={"Host": "localhost"}).status_code, 200, url)

    def test_badge_warns_when_collection_stopped_for_hours(self):
        old = "2000-01-01T00:00:00Z"
        self.assertEqual(status.badge({"collector": {"ok": True, "finished": old}})["tone"], "warn")
        self.assertEqual(status.badge({"collector": {"ok": True, "finished": status.now_iso()}})["tone"], "ok")
        self.assertEqual(status.badge({"collector": {"running": True}})["label"], "取得中…")


if __name__ == "__main__":
    unittest.main()
