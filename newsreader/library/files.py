"""Filesystem side of the article library: validating folders and resolving article files.

Everything here refuses symlinks and anything that resolves outside the folder it
was asked about, so an article record (or a crafted URL) can never make the app
read an arbitrary file.
"""
from __future__ import annotations

import time
from pathlib import Path

from bs4 import BeautifulSoup

from newsreader.sources import ALLOWED_EXTENSIONS, MAX_HTML_BYTES, ReaderError

MAX_FILE_COUNT = 400
MAX_TITLE_SCAN_BYTES = 128 * 1024


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
            files.append({
                "name": path.name,
                "display_name": path.stem,
                "title": _title_preview(path),
                "size": f"{stat.st_size / 1024:,.0f} KB",
                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                "timestamp": stat.st_mtime,
                "too_large": stat.st_size > MAX_HTML_BYTES,
            })
    except OSError as exc:
        raise ReaderError("ディレクトリを読み取れません。アクセス権を確認してください。") from exc
    files.sort(key=lambda f: f["timestamp"], reverse=True)
    return files[:MAX_FILE_COUNT]


def list_subdirectories(directory: Path) -> list[dict]:
    """List direct child folders for site-by-site article browsing."""
    try:
        folders = [
            {"name": path.name}
            for path in directory.iterdir()
            if path.is_dir() and not path.is_symlink()
        ]
    except OSError as exc:
        raise ReaderError("ディレクトリを読み取れません。アクセス権を確認してください。") from exc
    return sorted(folders, key=lambda folder: folder["name"].casefold())
