"""Phase 3: the index stores extracted bodies, syncs incrementally and migrates v3 databases.

Real-data checks run only when NEWS_REALDATA points at an ``articles`` folder; the
folder is copied to a temporary directory first, so nothing in it is modified.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from newsreader.library import index
from newsreader.sources import extract_article
from oracle import v3_library
from test_extractor import CNA, NAVER, NORMAL, VIDEO

FIXTURES = {("BBC", "a.html"): NORMAL, ("BBC", "v.html"): VIDEO, ("NAVER NEWS", "n.html"): NAVER, ("CNA", "c.html"): CNA}


class IndexTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "articles"
        for (site, name), data in FIXTURES.items():
            (self.root / site).mkdir(parents=True, exist_ok=True)
            (self.root / site / name).write_bytes(data)
        self.db = self.tmp / "library.sqlite3"
        index._last_sync.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ids(self):
        with closing(index.connect(self.db)) as db:
            return {(r["source"], r["filename"]): r["id"] for r in db.execute("SELECT id, source, filename FROM articles")}

    def bump(self, path: Path, data: bytes | None = None, seconds: int = 5):
        if data is not None:
            path.write_bytes(data)
        stamp = path.stat().st_mtime + seconds
        os.utime(path, (stamp, stamp))


class ContentTests(IndexTestCase):
    def test_stored_content_is_exactly_extract_article(self):
        index.sync(self.root, self.db, force=True)
        for (site, name), data in FIXTURES.items():
            record = index.find_by_file(self.db, site, name)
            self.assertEqual(index.get_content(self.db, record["id"]), extract_article(data), name)

    def test_list_and_get_never_carry_the_body(self):
        index.sync(self.root, self.db, force=True)
        item = index.list_articles(self.db)["items"][0]
        self.assertNotIn("content", item)
        self.assertNotIn("content", index.get(self.db, item["id"]))

    def test_corrupt_blob_reads_as_missing_and_is_reparsed(self):
        index.sync(self.root, self.db, force=True)
        rid = index.find_by_file(self.db, "CNA", "c.html")["id"]
        with closing(index.connect(self.db)) as db:
            db.execute("UPDATE articles SET content = x'00ff', content_key = NULL WHERE id = ?", (rid,))
        self.assertIsNone(index.get_content(self.db, rid))
        self.assertEqual(index.sync(self.root, self.db, force=True)["updated"], 1)
        self.assertEqual(index.get_content(self.db, rid), extract_article(CNA))


class IncrementalSyncTests(IndexTestCase):
    def test_only_changed_files_are_parsed(self):
        first = index.sync(self.root, self.db, force=True)
        self.assertEqual((first["added"], first["updated"], first["removed"]), (4, 0, 0))
        with mock.patch.object(index, "extract_article", side_effect=AssertionError("re-parsed")):
            again = index.sync(self.root, self.db, force=True)
        self.assertEqual((again["added"], again["updated"], again["removed"]), (0, 0, 0))
        before = self.ids()
        self.bump(self.root / "CNA" / "c.html", CNA.replace(b"First CNA", b"Changed CNA"))
        changed = index.sync(self.root, self.db, force=True)
        self.assertEqual((changed["added"], changed["updated"]), (0, 1))
        self.assertEqual(self.ids(), before, "ids must survive re-parsing (bookmarks are /a/<id>)")
        rid = before[("CNA", "c.html")]
        self.assertEqual(index.get_content(self.db, rid)["paragraphs"][0], "Changed CNA paragraph.")

    def test_touching_mtime_alone_reparses(self):
        index.sync(self.root, self.db, force=True)
        self.bump(self.root / "BBC" / "a.html")
        self.assertEqual(index.sync(self.root, self.db, force=True)["updated"], 1)

    def test_parser_version_bump_reparses_everything_in_place(self):
        index.sync(self.root, self.db, force=True)
        before = self.ids()
        with mock.patch.object(index, "PARSER_VERSION", index.PARSER_VERSION + 1):
            result = index.sync(self.root, self.db, force=True)
        self.assertEqual(result["updated"], 4)
        self.assertEqual(self.ids(), before)

    def test_removed_file_drops_its_row(self):
        index.sync(self.root, self.db, force=True)
        (self.root / "CNA" / "c.html").unlink()
        self.assertEqual(index.sync(self.root, self.db, force=True)["removed"], 1)
        self.assertIsNone(index.find_by_file(self.db, "CNA", "c.html"))

    def test_every_file_vanishing_keeps_the_index_and_says_why(self):
        index.sync(self.root, self.db, force=True)
        for (site, name) in FIXTURES:
            (self.root / site / name).unlink()
        result = index.sync(self.root, self.db, force=True)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(result["warning"])
        self.assertEqual(index.list_articles(self.db)["total"], 4)
        self.assertTrue(index.sync_state(self.db)["warning"])

    def test_missing_root_raises_reader_error_and_keeps_rows(self):
        index.sync(self.root, self.db, force=True)
        with self.assertRaises(index.ReaderError):
            index.sync(self.tmp / "nowhere", self.db, force=True)
        self.assertEqual(index.list_articles(self.db)["total"], 4)
        self.assertTrue(index.sync_state(self.db)["error"])

    def test_file_changing_while_read_is_deferred_not_stored(self):
        original_stat = Path.stat
        target = self.root / "CNA" / "c.html"

        def unstable(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == target:
                return os.stat_result((result.st_mode, result.st_ino, result.st_dev, result.st_nlink, result.st_uid,
                                       result.st_gid, result.st_size + 1, result.st_atime, result.st_mtime, result.st_ctime))
            return result

        with mock.patch.object(Path, "stat", unstable):
            result = index.sync(self.root, self.db, force=True)
        self.assertEqual((result["added"], result["deferred"]), (3, 1))
        self.assertIsNone(index.find_by_file(self.db, "CNA", "c.html"))
        self.assertEqual(index.sync(self.root, self.db, force=True)["added"], 1)

    def test_a_crashing_parser_marks_one_file_and_indexes_the_rest(self):
        real = extract_article

        def flaky(data):
            if b"CNA headline" in data:
                raise RecursionError("pathological nesting")
            return real(data)

        with mock.patch.object(index, "extract_article", flaky):
            result = index.sync(self.root, self.db, force=True)
        self.assertEqual((result["added"], result["errors"]), (4, 1))
        self.assertIn("RecursionError", index.find_by_file(self.db, "CNA", "c.html")["error"])
        self.assertEqual(len(index.list_articles(self.db)["items"]), 3)

    def test_oversized_file_is_an_error_row_without_reading_it(self):
        big = self.root / "BBC" / "big.html"
        big.write_bytes(b"x")
        info = index.scan_library(self.root)[("BBC", "big.html")]
        with mock.patch.object(index, "MAX_HTML_BYTES", 0):
            row = index._parse_file(info)
        self.assertEqual(row["error"], "サイズ制限超過")

    def test_asset_folders_and_symlinks_are_not_articles(self):
        (self.root / "Saved_files").mkdir()
        (self.root / "Saved_files" / "ad.html").write_bytes(NORMAL)  # browser "Save As" asset folder
        (self.root / "BBC" / "a_files").mkdir()
        (self.root / "BBC" / "a_files" / "iframe.html").write_bytes(NORMAL)
        (self.root / "BBC" / "a_media").mkdir()
        try:
            (self.root / "Linked").symlink_to(self.root / "BBC", target_is_directory=True)
            (self.root / "BBC" / "link.html").symlink_to(self.root / "BBC" / "a.html")
        except (OSError, NotImplementedError):
            pass  # symlinks may need privileges on Windows; the asset-folder part still runs
        found = index.scan_library(self.root)
        self.assertEqual(sorted(found), sorted(FIXTURES))

    def test_throttle_applies_only_without_force(self):
        index.sync(self.root, self.db)
        self.assertTrue(index.sync(self.root, self.db)["skipped"])
        self.assertFalse(index.sync(self.root, self.db, force=True)["skipped"])


class SchemaTests(IndexTestCase):
    def test_v3_database_migrates_in_place_keeping_ids(self):
        v3_library.sync(self.root, self.db, force=True)  # a genuine v3 index
        with closing(sqlite3.connect(self.db)) as raw:
            v3_ids = dict(((s, f), i) for i, s, f in raw.execute("SELECT id, source, filename FROM articles"))
            self.assertNotIn("content", {r[1] for r in raw.execute("PRAGMA table_info(articles)")})
        self.assertIsNone(index.get_content(self.db, v3_ids[("CNA", "c.html")]), "no body until first sync")
        result = index.sync(self.root, self.db, force=True)
        self.assertEqual((result["added"], result["updated"]), (0, 4))
        self.assertEqual(self.ids(), v3_ids)
        for key, data in FIXTURES.items():
            self.assertEqual(index.get_content(self.db, v3_ids[key]), extract_article(data))
        with closing(sqlite3.connect(self.db)) as raw:
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], index.SCHEMA_VERSION)

    def test_rolling_back_to_v3_and_forward_again_never_serves_stale_bodies(self):
        index.sync(self.root, self.db, force=True)
        target = self.root / "CNA" / "c.html"
        self.bump(target, CNA.replace(b"First CNA", b"Edited under v3"))
        v3_library._last_sync.clear()
        v3_library.sync(self.root, self.db, force=True)  # v3 updates size/mtime but knows nothing of content
        index.sync(self.root, self.db, force=True)
        rid = index.find_by_file(self.db, "CNA", "c.html")["id"]
        self.assertEqual(index.get_content(self.db, rid)["paragraphs"][0], "Edited under v3 paragraph.")

    def test_deleting_the_index_file_while_running_rebuilds_it(self):
        index.sync(self.root, self.db, force=True)
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)
        self.assertEqual(index.list_articles(self.db)["total"], 0)  # no "no such table"
        self.assertEqual(index.sync(self.root, self.db, force=True)["added"], 4)

    def test_index_is_in_wal_mode_including_after_being_recreated(self):
        index.sync(self.root, self.db, force=True)
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)
        index.list_articles(self.db)
        with closing(sqlite3.connect(self.db)) as raw:
            self.assertEqual(raw.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_schema_check_is_one_pragma_when_current(self):
        index.sync(self.root, self.db, force=True)
        statements = []
        with closing(index.connect(self.db)) as db:
            db.set_trace_callback(statements.append)
            index._ensure_schema(db)
        self.assertEqual(statements, ["PRAGMA user_version"])


class ConcurrencyTests(IndexTestCase):
    def test_readers_and_two_syncers_do_not_collide(self):
        for n in range(40):
            (self.root / "CNA" / f"extra{n}.html").write_bytes(CNA.replace(b"CNA headline", b"Headline %d" % n))
        errors = []

        def syncer():
            try:
                for _ in range(3):
                    index.sync(self.root, self.db, force=True)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        def reader():
            try:
                deadline = time.time() + 2
                while time.time() < deadline:
                    index.list_articles(self.db)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=syncer) for _ in range(2)] + [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(index.list_articles(self.db)["total"], 44)

    def test_background_sync_indexes_retries_and_stops(self):
        missing = self.tmp / "later"

        def root():
            from newsreader.library.files import validate_directory
            return validate_directory(missing)

        worker = index.start_background_sync(root, self.db, interval=30)
        try:
            for _ in range(100):
                if index.sync_state(self.db).get("error"):
                    break
                time.sleep(0.02)
            self.assertTrue(index.sync_state(self.db)["error"], "a missing folder is reported, not fatal")
            shutil.copytree(self.root, missing)
            worker.trigger()
            for _ in range(250):
                if index.list_articles(self.db)["total"] == 4:
                    break
                time.sleep(0.02)
            self.assertEqual(index.list_articles(self.db)["total"], 4)
            self.assertEqual(index.sync_state(self.db)["error"], "")
        finally:
            worker.stop()
        self.assertFalse(worker.thread.is_alive())


@unittest.skipUnless(os.environ.get("NEWS_REALDATA"), "NEWS_REALDATA not set")
class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "articles"
        source = Path(os.environ["NEWS_REALDATA"])
        cls.root.mkdir()
        for site in source.iterdir():
            if site.is_dir():
                (cls.root / site.name).mkdir()
                for html in site.glob("*.html"):
                    shutil.copy2(html, cls.root / site.name / html.name)
        cls.db = cls.tmp / "library.sqlite3"
        index._last_sync.clear()
        started = time.perf_counter()
        cls.first = index.sync(cls.root, cls.db, force=True)
        cls.first_seconds = time.perf_counter() - started

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_stored_body_equals_a_fresh_extraction(self):
        self.assertEqual(self.first["added"], len(list(self.root.glob("*/*.html"))))
        with closing(index.connect(self.db)) as db:
            rows = db.execute("SELECT id, source, filename FROM articles").fetchall()
        for rid, site, name in rows:
            fresh = extract_article((self.root / site / name).read_bytes())
            self.assertEqual(index.get_content(self.db, rid), fresh, f"{site}/{name}")

    def test_performance_budget(self):
        started = time.perf_counter()
        again = index.sync(self.root, self.db, force=True)
        unchanged = time.perf_counter() - started
        ids = [r["id"] for r in index.list_articles(self.db, per_page=200)["items"]]
        started = time.perf_counter()
        for n in range(200):
            index.get_content(self.db, ids[n % len(ids)])
        reads = time.perf_counter() - started
        self.assertEqual((again["added"], again["updated"]), (0, 0))
        # Generous ceilings (CI machines vary); the measured values are printed for the record.
        self.assertLess(unchanged, 0.5)
        self.assertLess(reads, 1.0)
        print(f"\n[perf] first sync {self.first_seconds:.2f}s, unchanged sync {unchanged * 1000:.1f}ms, "
              f"200 bodies {reads * 1000:.1f}ms, index {index.index_stats(self.db)['bytes'] / 1024:.0f}KB")


if __name__ == "__main__":
    unittest.main()
