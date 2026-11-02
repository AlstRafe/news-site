"""Library maintenance (§9, D-11): report / slim / prune / compact. Dry run unless ``--apply``.

- ``report``   sizes per site, asset folders, index and cache files (read-only).
- ``slim``     rewrite saved pages without scripts/styles/…, one file at a time, only when the
               extracted article stays identical (sources/slim.py). Originals are copied to a
               backup folder first unless ``--no-backup``. File times are kept.
- ``prune``    move ``*_files`` folders (what a browser's "Save As" adds; this app never reads
               them) and image caches whose article is gone (orphan ``*_media``) to a backup
               folder; ``--delete`` removes them instead.
- ``compact``  VACUUM the index and the translation cache.

Safety: nothing changes without ``--apply``; only paths inside the library root are touched;
symlinks are never followed or modified; the backup folder may not lie inside the library
(the app would index the copies). Every run prints what it did or would do.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from newsreader import paths
from newsreader.library.files import ASSET_DIR_SUFFIXES, scan_library
from newsreader.sources import ReaderError, extract_article
from newsreader.sources.slim import slim_html


@dataclass
class Outcome:
    lines: list[str] = field(default_factory=list)
    changed: int = 0
    errors: int = 0

    def say(self, text: str) -> None:
        self.lines.append(text)


def _mb(size: int) -> str:
    return f"{size / 1_000_000:,.1f}MB"


def _tree_size(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            total += info.st_size
    return total


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _asset_dirs(root: Path) -> list[tuple[Path, str]]:
    """(folder, owning html name) for every ``*_files`` / ``*_media`` folder in root or a site folder."""
    found = []
    for parent in [root] + [p for p in sorted(root.iterdir()) if p.is_dir() and not p.is_symlink()
                            and not p.name.endswith(ASSET_DIR_SUFFIXES)]:
        try:
            children = sorted(parent.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink() or not child.is_dir():
                continue
            for suffix in ASSET_DIR_SUFFIXES:
                if child.name.endswith(suffix):
                    found.append((child, child.name[: -len(suffix)]))
    return found


def _has_article(folder: Path, stem: str) -> bool:
    return any((folder.parent / f"{stem}{ext}").is_file() for ext in (".html", ".htm", ".HTML", ".HTM"))


def default_backup(root: Path, kind: str) -> Path:
    return root.resolve().parent / "bkup" / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}"


def _sqlite_free_bytes(path: Path) -> int | None:
    """Free pages x page size, read from the file header — opening the database (even read-only)
    would create -wal/-shm files, and ``report`` must not create anything."""
    try:
        with path.open("rb") as handle:
            header = handle.read(100)
    except OSError:
        return None
    if len(header) < 100 or not header.startswith(b"SQLite format 3\x00"):
        return None
    page_size = int.from_bytes(header[16:18], "big")
    page_size = 65536 if page_size == 1 else page_size
    return page_size * int.from_bytes(header[36:40], "big")


# ---- commands ------------------------------------------------------------------

def report(root: Path, db_path: Path, cache_path: Path) -> Outcome:
    out = Outcome()
    files = scan_library(root)
    per_site: dict[str, list[int]] = {}
    for (source, _), info in files.items():
        row = per_site.setdefault(source or "(直下)", [0, 0])
        row[0] += 1
        row[1] += info.size
    out.say(f"記事ライブラリ: {root}")
    for site, (count, size) in sorted(per_site.items()):
        out.say(f"  {site}: {count:,} 本 {_mb(size)}（平均 {size / max(1, count) / 1024:,.0f}KB）")
    out.say(f"  合計: {len(files):,} 本 {_mb(sum(i.size for i in files.values()))}")
    saved_pages = media = orphans = 0
    saved_size = media_size = orphan_size = 0
    for folder, stem in _asset_dirs(root):
        size = _tree_size(folder)
        if folder.name.endswith("_files"):
            saved_pages, saved_size = saved_pages + 1, saved_size + size
        else:
            media, media_size = media + 1, media_size + size
            if not _has_article(folder, stem):
                orphans, orphan_size = orphans + 1, orphan_size + size
    out.say(f"  ブラウザ保存の付属フォルダ（*_files、アプリは不使用）: {saved_pages:,} 個 {_mb(saved_size)}")
    out.say(f"  画像キャッシュ（*_media）: {media:,} 個 {_mb(media_size)}（記事が無いもの {orphans:,} 個 {_mb(orphan_size)}）")
    for label, path in (("索引", db_path), ("翻訳キャッシュ", cache_path)):
        if path.is_file():
            free = ""
            unused = _sqlite_free_bytes(path)
            if unused is not None:
                free = f"（未使用領域 {_mb(unused)}）"
            out.say(f"  {label}: {path.name} {_mb(path.stat().st_size)}{free}")
    return out


def slim(root: Path, *, apply: bool, backup: Path | None, limit: int = 0) -> Outcome:
    out = Outcome()
    files = scan_library(root)
    before = after = done = same = 0
    for (source, name), info in sorted(files.items()):
        if limit and done + same >= limit:
            break
        path = info.path
        try:
            raw = path.read_bytes()
            article = extract_article(raw)
        except (OSError, ReaderError):
            continue  # unreadable or not an article: never touched
        try:
            small = slim_html(raw)
            equal = len(small) < len(raw) and extract_article(small) == article
        except (ReaderError, ValueError, RecursionError):
            small, equal = raw, False
        if not equal:
            same += 1
            if len(small) < len(raw):
                out.say(f"  そのまま（軽量化すると抽出結果が変わる）: {source}/{name}")
            continue
        done += 1
        before, after = before + len(raw), after + len(small)
        if not apply:
            continue
        try:
            if backup is not None:
                target = backup / path.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            temporary = path.with_name(path.name + ".slim-tmp")
            temporary.write_bytes(small)
            os.utime(temporary, ns=(info.mtime_ns, info.mtime_ns))  # keep the file's time
            os.replace(temporary, path)
            out.changed += 1
        except OSError as exc:
            out.errors += 1
            out.say(f"  失敗: {source}/{name}（{type(exc).__name__}）")
    verb = "軽量化しました" if apply else "軽量化できます（ドライラン）"
    out.lines.insert(0, f"{verb}: {done:,} 本 {_mb(before)} → {_mb(after)}"
                        f"（{(1 - after / before) * 100 if before else 0:.0f}% 削減）。抽出結果が変わるため対象外: {same:,} 本")
    if apply and backup is not None and out.changed:
        out.say(f"  元のファイル: {backup}（確認後に削除して構いません）")
    return out


def prune(root: Path, *, apply: bool, backup: Path | None, delete: bool) -> Outcome:
    out = Outcome()
    targets = []
    for folder, stem in _asset_dirs(root):
        if folder.name.endswith("_files"):
            targets.append((folder, "ブラウザ保存の付属フォルダ"))
        elif not _has_article(folder, stem):
            targets.append((folder, "記事が無い画像キャッシュ"))
    total = 0
    for folder, _why in targets:
        size = _tree_size(folder)
        total += size
        if not apply:
            continue
        try:
            if delete:
                shutil.rmtree(folder)
            else:
                target = backup / folder.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(folder), str(target))
            out.changed += 1
        except OSError as exc:
            out.errors += 1
            out.say(f"  失敗: {folder.relative_to(root)}（{type(exc).__name__}）")
    action = ("削除しました" if delete else f"移動しました → {backup}") if apply else (
        "削除できます（ドライラン）" if delete else "移動できます（ドライラン）")
    out.lines.insert(0, f"{action}: {len(targets):,} フォルダ {_mb(total)}")
    for folder, why in targets[:20]:
        out.say(f"  {why}: {folder.relative_to(root)}")
    if len(targets) > 20:
        out.say(f"  …ほか {len(targets) - 20} 個")
    return out


def compact(paths_to_compact: list[Path], *, apply: bool) -> Outcome:
    out = Outcome()
    for path in paths_to_compact:
        if not path.is_file() or path.is_symlink():
            continue
        size = path.stat().st_size
        if not apply:
            out.say(f"VACUUM できます（ドライラン）: {path.name} {_mb(size)}")
            continue
        try:
            with closing(sqlite3.connect(path, timeout=5)) as db:
                db.execute("VACUUM")
                db.execute("PRAGMA optimize")
            out.changed += 1
            out.say(f"VACUUM しました: {path.name} {_mb(size)} → {_mb(path.stat().st_size)}")
        except sqlite3.Error as exc:  # busy (collector or app writing): try again later
            out.errors += 1
            out.say(f"VACUUM できませんでした（使用中の可能性）: {path.name}（{type(exc).__name__}）")
    return out


# ---- command line ----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="maintenance.py", description="記事ライブラリの整理（--apply が無ければ何も変更しない）")
    parser.add_argument("command", choices=("report", "slim", "prune", "compact"))
    parser.add_argument("--root", default="", help="記事フォルダ（既定: HTML_DIRECTORY、なければ articles/）")
    parser.add_argument("--apply", action="store_true", help="実際に変更する（無ければドライラン）")
    parser.add_argument("--backup", default="", help="元ファイルの退避先（既定: 記事フォルダの隣の bkup/<種類>-<日時>）")
    parser.add_argument("--no-backup", action="store_true", help="slim: 元ファイルを残さない")
    parser.add_argument("--delete", action="store_true", help="prune: 移動せず削除する")
    parser.add_argument("--limit", type=int, default=0, help="slim: 最大件数（試しに少しだけ）")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else paths.articles_dir()
    if root.is_symlink() or not root.is_dir():
        print(f"記事フォルダが見つからないか、シンボリックリンクです: {root}", file=sys.stderr)
        return 2
    root = root.resolve()
    backup = None
    if args.command in ("slim", "prune") and not (args.command == "slim" and args.no_backup) \
            and not (args.command == "prune" and args.delete):
        backup = Path(args.backup).expanduser().resolve() if args.backup else default_backup(root, args.command)
        if _inside(backup, root):
            print("退避先を記事フォルダの中にはできません（コピーが記事として索引されるため）。", file=sys.stderr)
            return 2

    from newsreader.library import index
    from newsreader.services import cache
    if args.command == "report":
        out = report(root, index.DEFAULT_DB_PATH, cache.CACHE_PATH)
    elif args.command == "slim":
        out = slim(root, apply=args.apply, backup=backup, limit=args.limit)
    elif args.command == "prune":
        out = prune(root, apply=args.apply, backup=backup, delete=args.delete)
    else:
        out = compact([index.DEFAULT_DB_PATH, cache.CACHE_PATH], apply=args.apply)
    print("\n".join(out.lines))
    if not args.apply and args.command != "report":
        print("\n何も変更していません。実行するには --apply を付けてください。")
    return 1 if out.errors else 0
