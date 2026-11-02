"""Filesystem side of the article library: validating folders and resolving article files.

Everything here refuses symlinks and anything that resolves outside the folder it
was asked about, so an article record (or a crafted URL) can never make the app
read an arbitrary file.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from newsreader.sources import ALLOWED_EXTENSIONS, MAX_HTML_BYTES, ReaderError

MAX_FILE_COUNT = 400
MAX_TITLE_SCAN_BYTES = 128 * 1024

#: Folders that sit beside saved articles but never hold articles themselves:
#: ``<name>_files`` is what a browser's "Save As" writes (it can contain ad
#: iframes saved as .html), ``<name>_media`` is this app's image cache.
ASSET_DIR_SUFFIXES = ("_files", "_media")


@dataclass(frozen=True)
class FileInfo:
    path: Path
    size: int
    mtime: float
    mtime_ns: int


def is_asset_dir(name: str) -> bool:
    return name.endswith(ASSET_DIR_SUFFIXES)


def _is_article_entry(entry: os.DirEntry) -> bool:
    return (
        not entry.is_symlink()
        and entry.is_file(follow_symlinks=False)
        and os.path.splitext(entry.name)[1].lower() in ALLOWED_EXTENSIONS
    )


def scan_library(root: Path) -> dict[tuple[str, str], FileInfo]:
    """Map (source, filename) -> file info for ``root/*.html`` and ``root/<source>/*.html``.

    One level of site folders, exactly like v3. Symlinks are skipped at every level
    and asset folders (``*_files``, ``*_media``) are never treated as sites.
    ``os.scandir`` is used because on Windows it returns size and mtime from the
    directory listing itself, without one extra system call per file.
    """
    found: dict[tuple[str, str], FileInfo] = {}

    def add(source: str, entry: os.DirEntry) -> None:
        try:
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            return  # vanished or unreadable between listing and stat: picked up next time
        found[(source, entry.name)] = FileInfo(Path(entry.path), stat.st_size, stat.st_mtime, stat.st_mtime_ns)

    try:
        with os.scandir(root) as top:
            entries = list(top)
    except OSError as exc:
        raise ReaderError("記事ライブラリを読み取れません。") from exc
    for entry in entries:
        if entry.is_symlink():
            continue
        if _is_article_entry(entry):
            add("", entry)
        elif entry.is_dir(follow_symlinks=False) and not is_asset_dir(entry.name):
            try:
                with os.scandir(entry.path) as children:
                    for child in children:
                        if _is_article_entry(child):
                            add(entry.name, child)
            except OSError:
                continue  # one unreadable site folder must not hide the others
    return found


def validate_directory(directory: str | Path) -> Path:
    text = str(directory).strip().strip('"')
    if not text:
        raise ReaderError("ディレクトリのパスを入力してください。")
    try:
        path = Path(text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise ReaderError("指定したディレクトリが見つかりません。") from None
    if not path.is_dir():
        raise ReaderError("指定したパスはディレクトリではありません。")
    return path


def resolve_html_file(directory: Path, filename: str) -> Path:
    if not filename or Path(filename).name != filename or Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ReaderError("HTMLファイル名が不正です。")
    path = directory / filename
    try:
        if path.is_symlink() or not path.is_file() or path.resolve(strict=True).parent != directory.resolve():
            raise ReaderError("選択されたHTMLファイルが見つかりません。")
        if path.stat().st_size > MAX_HTML_BYTES:
            raise ReaderError("HTMLファイルが大きすぎます（最大12MB）。")
    except OSError as exc:
        raise ReaderError("HTMLファイルを読み取れません。") from exc
    return path


# ---- Folder browsing helpers ------------------------------------------------
# Used by the folder-picker UI that v3 replaced with the URL-based list. They
# stay (with their tests) because reviving that UI is an open question (C-01).


def _title_preview(path: Path) -> str:
    try:
        with path.open("rb") as html_file:
            soup = BeautifulSoup(html_file.read(MAX_TITLE_SCAN_BYTES), "html.parser")
        for selector in ("#bbc-main h1", "#main-content h1", "#title_area", "article.article h1"):
            title = soup.select_one(selector)
            if title and title.get_text(" ", strip=True):
                return title.get_text(" ", strip=True)
        meta = soup.select_one('meta[property="og:title"]')
        if meta and meta.get("content"):
            return str(meta["content"]).strip()
        return soup.title.get_text(" ", strip=True) if soup.title else ""
    except (OSError, ValueError):
        return ""


def list_html_files(directory: Path) -> list[dict]:
    """List only direct children, not symlinks or subdirectories."""
    files = []
    try:
        for path in directory.iterdir():
            if path.suffix.lower() not in ALLOWED_EXTENSIONS or path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            files.append(
                {
                    "name": path.name,
                    "display_name": path.stem,
                    "title": _title_preview(path),
                    "size": f"{stat.st_size / 1024:,.0f} KB",
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                    "timestamp": stat.st_mtime,
                    "too_large": stat.st_size > MAX_HTML_BYTES,
                }
            )
    except OSError as exc:
        raise ReaderError("ディレクトリを読み取れません。アクセス権を確認してください。") from exc
    files.sort(key=lambda f: f["timestamp"], reverse=True)
    return files[:MAX_FILE_COUNT]


def list_subdirectories(directory: Path) -> list[dict]:
    """List direct child folders for site-by-site article browsing."""
    try:
        folders = [{"name": path.name} for path in directory.iterdir() if path.is_dir() and not path.is_symlink()]
    except OSError as exc:
        raise ReaderError("ディレクトリを読み取れません。アクセス権を確認してください。") from exc
    return sorted(folders, key=lambda folder: folder["name"].casefold())
