"""Extract article text from saved BBC, NAVER NEWS, and CNA HTML."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

MAX_HTML_BYTES = 12 * 1024 * 1024
MAX_FILE_COUNT = 400
MAX_TITLE_SCAN_BYTES = 128 * 1024
ALLOWED_EXTENSIONS = {".html", ".htm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class ReaderError(Exception):
    """Safe-to-display validation / extraction problem."""


_KST = timezone(timedelta(hours=9))


def _to_utc_iso(value: str, naive_tz: timezone = timezone.utc) -> str:
    """Normalise a page timestamp to ``YYYY-MM-DDTHH:MM:SSZ`` (UTC); "" if unparseable."""
    text = (value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=naive_tz)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_published(soup: BeautifulSoup) -> str:
    """Best-effort publication time (UTC ISO) from meta, JSON-LD, NAVER or <time>."""
    meta = soup.select_one('meta[property="article:published_time"], meta[name="article:published_time"]')
    if meta and _to_utc_iso(str(meta.get("content", ""))):
        return _to_utc_iso(str(meta["content"]))
    for script in soup.find_all("script", type="application/ld+json"):
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string or "")
        if match and _to_utc_iso(match.group(1)):
            return _to_utc_iso(match.group(1))
    # NAVER prints local Korean time without a UTC offset.
    naver = soup.select_one("span._ARTICLE_DATE_TIME, span.media_end_head_info_datestamp_time")
    if naver:
        stamp = _to_utc_iso(str(naver.get("data-date-time", "")), naive_tz=_KST)
        if stamp:
            return stamp
    time_tag = soup.select_one("time[datetime]")
    return _to_utc_iso(str(time_tag.get("datetime", ""))) if time_tag else ""


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


def _image_source(image) -> str:
    candidates = []
    for item in str(image.get("srcset", "")).split(","):
        pieces = item.strip().split()
        if pieces:
            size = 0
            if len(pieces) > 1:
                try:
                    size = int(pieces[1].rstrip("w"))
                except ValueError:
                    pass
            candidates.append((size, pieces[0]))
    if candidates:
        usable = [candidate for candidate in candidates if candidate[0] <= 1600 and candidate[0] > 0]
        if usable:
            return max(usable)[1]
        if candidates:
            return min(candidates)[1]
    return str(image.get("data-src") or image.get("data-original") or image.get("src") or "").strip()


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


def extract_article(html: bytes) -> dict:
    """Extract title and body paragraphs from supported saved news pages.

    Saved HTML is parsed as *data*. Nothing from it is rendered as raw HTML or
    evaluated as JavaScript in the Flask template.
    """
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    og_url = soup.select_one('meta[property="og:url"]')
    page_url = str(canonical.get("href", "")) if canonical else ""
    if not page_url and og_url:
        page_url = str(og_url.get("content", ""))
    host = (urlsplit(page_url).hostname or "").lower()

    page_meta = soup.select_one('meta[name="pagetype"]')
    bbc_page_type = str(page_meta.get("content", "")) if page_meta else ""
    is_bbc_host = host in {"www.bbc.com", "www.bbc.co.uk"}
    is_bbc_video = (
        bbc_page_type == "Video Item"
        or bool(soup.select_one('[data-testid="video-page-video-section"]'))
        or (is_bbc_host and "/news/videos/" in urlsplit(page_url).path)
    )
    is_bbc_article = (
        bbc_page_type == "News Article"
        or bool(soup.select_one('article [data-component="layout-block"]'))
        or (is_bbc_host and bool(soup.select_one("#main-content article")))
    )
    is_naver = host == "n.news.naver.com" or bool(soup.select_one("#dic_area"))
    is_cna = host in {"cna.com.tw", "www.cna.com.tw"} or bool(soup.select_one("article.article .centralContent"))

    if is_bbc_video or is_bbc_article:
        if is_bbc_video:
            kind = "動画記事（説明文）"
            selector = '[data-testid="video-page-video-section"] div[class*="TextStyled"] > p[class*="ParagraphStyled"]'
            image_selector = '[data-testid="video-page-video-section"] img, #main-content article [data-testid="image"] img'
        else:
            kind = "通常記事"
            if soup.select_one('article [data-component="layout-block"]'):
                selector = 'article [data-component="layout-block"] p[class*="ParagraphStyled"]'
                image_selector = '#bbc-main [data-component*="image"] img, #bbc-main figure img'
            else:
                selector = '#main-content article [data-testid="rich-text"] p[class*="Paragraph"]'
                image_selector = '#main-content article [data-testid="image"] img'
        title_tag = soup.select_one("#bbc-main h1") or soup.select_one("#main-content h1")
        paragraphs = [p.get_text(" ", strip=True) for p in soup.select(selector)]
        paragraphs = [p for p in paragraphs if p and p != "More on this story."]
        host_allowed = host in {"www.bbc.com", "www.bbc.co.uk"}
        page_type = bbc_page_type
    elif is_naver:
        kind = "NAVER NEWS"
        title_tag = soup.select_one("#title_area")
        body = soup.select_one("#dic_area")
        if body:
            # NAVER stores paragraph breaks as pairs of <br> tags.
            paragraphs = [part.strip() for part in body.get_text("\n").splitlines() if part.strip()]
        else:
            paragraphs = []
        image_selector = "#img_area img, #dic_area img, #newsct_article figure img"
        host_allowed = host in {"n.news.naver.com", "news.naver.com"}
        page_type = "NAVER NEWS"
    elif is_cna:
        kind = "中央社 CNA"
        title_tag = soup.select_one("article.article h1")
        if title_tag is None:
            title_tag = soup.select_one("h1")
        paragraphs = [
            p.get_text(" ", strip=True)
            for p in soup.select(
                "article.article .centralContent .paragraph:not(.moreArticle):not(.appDownload) > p"
            )
        ]
        image_selector = "article.article .centralContent img"
        host_allowed = host in {"cna.com.tw", "www.cna.com.tw"}
        page_type = "中央社 CNA"
    else:
        raise ReaderError("未対応のHTML形式です。BBC、NAVER NEWS、中央社の記事に対応しています。")

    fallback = soup.select_one('meta[property="og:title"]') or soup.select_one("title")
    if title_tag:
        title = title_tag.get_text(" ", strip=True)
    elif fallback is None:
        title = "タイトル不明"
    elif fallback.name == "meta":
        title = str(fallback.get("content", "タイトル不明"))
    else:
        title = fallback.get_text(" ", strip=True)
    if not paragraphs:
        raise ReaderError("本文が見つかりません。保存したHTMLの構造を確認してください。")

    images = []
    seen_images = set()
    for image in soup.select(image_selector):
        if image.find_parent(class_=("appDownload", "gmailNewsWrapper", "btn_support")):
            continue
        source = _image_source(image)
        if not source:
            continue
        source = urljoin(page_url, source)
        parsed_source = urlsplit(source)
        if parsed_source.scheme not in {"http", "https"}:
            continue
        if "/website/img/" in parsed_source.path:
            continue
        if Path(parsed_source.path).suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if source in seen_images:
            continue
        seen_images.add(source)
        images.append({"src": source, "alt": str(image.get("alt", "")).strip()})
        if len(images) >= 8:
            break
    if not images:
        og_image = soup.select_one('meta[property="og:image"]') or soup.select_one('meta[name="twitter:image"]')
        if og_image:
            source = urljoin(page_url, str(og_image.get("content", "")).strip())
            parsed_source = urlsplit(source)
            if parsed_source.scheme in {"http", "https"} and Path(parsed_source.path).suffix.lower() in IMAGE_EXTENSIONS:
                images.append({"src": source, "alt": title})

    if not host_allowed or not page_url.startswith("https://"):
        url = ""
    else:
        url = page_url

    return {
        "title": title,
        "kind": kind,
        "page_type": page_type,
        "paragraphs": paragraphs,
        "paragraph_count": len(paragraphs),
        "body": "\n\n".join(paragraphs),
        "url": url,
        "images": images,
        "published": extract_published(soup),
    }
