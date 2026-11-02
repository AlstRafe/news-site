"""Phase 8: maintenance commands — dry run by default, only inside the root, never via symlinks,
slimming only when extraction stays identical. Everything runs on a temp copy."""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_extractor import CNA, NAVER, NORMAL

from newsreader.library import index, maintenance
from newsreader.sources import extract_article
from newsreader.sources.slim import slim_html, slim_if_equivalent

FAT = NORMAL.replace(b"<head>", b'<head><link rel="canonical" href="https://www.bbc.com/news/articles/c1">'
                                 b"<script>" + b"var x = 1;" * 3000 + b"</script><style>p{color:red}</style>")
# Text inside an inline <svg> is extracted; slimming would remove it -> must stay untouched.
KEEP = CNA.replace(b"<p>Second CNA paragraph.</p>", b"<p>Chart<svg><text>2026</text></svg></p>").replace(
    b"</article>", b"<script>" + b"y" * 4000 + b"</script></article>")


def tree_digest(root: Path) -> dict[str, str]:
    out = {}
    for dirpath, _, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest() if not path.is_symlink() else "link"
    return out


class MaintenanceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "articles"
        (self.root / "BBC").mkdir(parents=True)
        (self.root / "CNA").mkdir()
        (self.root / "NAVER NEWS").mkdir()
        (self.root / "BBC" / "fat.html").write_bytes(FAT)
        (self.root / "CNA" / "keep.html").write_bytes(KEEP)
        (self.root / "NAVER NEWS" / "n.html").write_bytes(NAVER)
        (self.root / "BBC" / "fat_files").mkdir()
        (self.root / "BBC" / "fat_files" / "ad.html").write_bytes(b"<html>ad</html>")
        (self.root / "BBC" / "fat_media").mkdir()
        (self.root / "BBC" / "fat_media" / "0.jpg").write_bytes(b"\xff\xd8" + b"0" * 100)
        (self.root / "BBC" / "gone_media").mkdir()
        (self.root / "BBC" / "gone_media" / "0.jpg").write_bytes(b"\xff\xd8" + b"1" * 100)
        os.utime(self.root / "BBC" / "fat.html", ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        self.backup = self.tmp / "bkup" / "test"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class DryRunTests(MaintenanceTestCase):
    def test_no_command_changes_anything_without_apply(self):
        before = tree_digest(self.tmp)
        db = self.tmp / "library.sqlite3"
        index.sync(self.root, db, force=True)
        before_db = tree_digest(self.tmp)
        from unittest import mock
        # The commands must see this index (a WAL database): even opening it read-only would
        # leave -wal/-shm files behind, which a dry run must not do.
        with mock.patch.object(index, "DEFAULT_DB_PATH", db):
            for args in (["report"], ["slim"], ["prune"], ["prune", "--delete"], ["compact"], ["slim", "--no-backup"]):
                with self.subTest(args=args):
                    self.assertEqual(maintenance.main(args + ["--root", str(self.root)]), 0)
                    self.assertEqual(tree_digest(self.tmp), before_db)
        self.assertTrue(set(before) <= set(before_db))
        self.assertFalse((self.tmp / "bkup").exists())

    def test_report_counts(self):
        out = maintenance.report(self.root, self.tmp / "none.sqlite3", self.tmp / "none2.sqlite3")
        text = "\n".join(out.lines)
        self.assertIn("合計: 3 本", text)
        self.assertIn("*_files、アプリは不使用）: 1 個", text)
        self.assertIn("記事が無いもの 1 個", text)


class SlimTests(MaintenanceTestCase):
    def test_slim_function_keeps_extraction_and_is_much_smaller(self):
        small = slim_html(FAT)
        self.assertLess(len(small), len(FAT) // 4)
        self.assertEqual(extract_article(small), extract_article(FAT))
        self.assertEqual(slim_if_equivalent(KEEP), (KEEP, False))
        self.assertEqual(slim_if_equivalent(b"not an article"), (b"not an article", False))

    def test_apply_rewrites_only_equivalent_files_backs_up_and_keeps_time(self):
        before = extract_article(FAT)
        out = maintenance.slim(self.root, apply=True, backup=self.backup)
        self.assertEqual(out.errors, 0)
        fat = self.root / "BBC" / "fat.html"
        self.assertLess(fat.stat().st_size, len(FAT) // 4)
        self.assertEqual(extract_article(fat.read_bytes()), before)
        self.assertEqual(fat.stat().st_mtime_ns, 1_700_000_000_000_000_000)
        self.assertEqual((self.backup / "BBC" / "fat.html").read_bytes(), FAT)
        self.assertEqual((self.root / "CNA" / "keep.html").read_bytes(), KEEP)  # would change extraction
        self.assertIn("CNA/keep.html", "\n".join(out.lines))
        self.assertFalse(list(self.root.rglob("*.slim-tmp")))
        # A second run finds nothing more to do.
        again = maintenance.slim(self.root, apply=True, backup=self.backup)
        self.assertEqual(again.changed, 0)

    def test_symlinked_article_is_never_touched(self):
        outside = self.tmp / "outside.html"
        outside.write_bytes(FAT)
        try:
            (self.root / "BBC" / "link.html").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        maintenance.slim(self.root, apply=True, backup=self.backup)
        self.assertEqual(outside.read_bytes(), FAT)
        self.assertTrue((self.root / "BBC" / "link.html").is_symlink())

    def test_backup_inside_the_library_is_refused(self):
        code = maintenance.main(["slim", "--apply", "--root", str(self.root), "--backup", str(self.root / "bk")])
        self.assertEqual(code, 2)
        self.assertEqual((self.root / "BBC" / "fat.html").read_bytes(), FAT)

    def test_symlinked_root_is_refused(self):
        link = self.tmp / "lib-link"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        self.assertEqual(maintenance.main(["slim", "--apply", "--root", str(link)]), 2)
        self.assertEqual((self.root / "BBC" / "fat.html").read_bytes(), FAT)


class PruneTests(MaintenanceTestCase):
    def test_moves_saved_page_folders_and_orphan_caches_only(self):
        out = maintenance.prune(self.root, apply=True, backup=self.backup, delete=False)
        self.assertEqual(out.changed, 2)
        self.assertFalse((self.root / "BBC" / "fat_files").exists())
        self.assertFalse((self.root / "BBC" / "gone_media").exists())
        self.assertTrue((self.root / "BBC" / "fat_media" / "0.jpg").exists())  # its article exists
        self.assertTrue((self.backup / "BBC" / "fat_files" / "ad.html").exists())
        self.assertTrue((self.backup / "BBC" / "gone_media" / "0.jpg").exists())
        self.assertEqual((self.root / "BBC" / "fat.html").read_bytes(), FAT)

    def test_delete(self):
        maintenance.prune(self.root, apply=True, backup=None, delete=True)
        self.assertFalse((self.root / "BBC" / "fat_files").exists())
        self.assertFalse(self.backup.exists())

    def test_symlinked_asset_folder_is_not_followed(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        (target / "keep.txt").write_text("x")
        try:
            (self.root / "BBC" / "other_files").symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        maintenance.prune(self.root, apply=True, backup=None, delete=True)
        self.assertTrue((target / "keep.txt").exists())


class CompactTests(MaintenanceTestCase):
    def test_vacuum_keeps_the_index_usable(self):
        db = self.tmp / "library.sqlite3"
        index.sync(self.root, db, force=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE junk(x)")
            conn.executemany("INSERT INTO junk VALUES(?)", [("z" * 1000,)] * 500)
            conn.commit()
            conn.execute("DROP TABLE junk")
            conn.commit()
        big = db.stat().st_size
        out = maintenance.compact([db], apply=True)
        self.assertEqual(out.errors, 0)
        self.assertLess(db.stat().st_size, big)
        self.assertEqual(len(index.list_articles(db)["items"]), 3)


if __name__ == "__main__":
    unittest.main()
