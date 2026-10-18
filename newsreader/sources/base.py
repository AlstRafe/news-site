"""Building blocks shared by every site adapter.

A site adapter is a module with ``NAME``, ``matches(page)`` and ``parse(page)``
(see ``bbc.py``). It decides *what* to read from a page; this module decides
*how* the common parts (title fallback, images, canonical URL, publication time)
are turned into the article dictionary the rest of the app consumes.

Equivalence with the v3 single-file extractor is a hard requirement (Phase 2
acceptance: zero differences on the 130 real articles, plus differential tests
on truncated / mutated pages). Several expressions below therefore look odd on
purpose; each one is annotated where it mirrors a v3 quirk.

Saved HTML is parsed as *data*. Nothing from it is rendered as raw HTML or
evaluated as JavaScript.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

MAX_HTML_BYTES = 12 * 1024 * 1024
ALLOWED_EXTENSIONS = frozenset({".html", ".htm"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
MAX_IMAGES = 8

UNSUPPORTED_MESSAGE = "未対応のHTML形式です。BBC、NAVER NEWS、中央社の記事に対応しています。"
NO_BODY_MESSAGE = "本文が見つかりません。保存したHTMLの構造を確認してください。"
UNKNOWN_TITLE = "タイトル不明"

# Images inside these containers are site chrome (app banners, share widgets), not article images.
_CHROME_IMAGE_PARENTS = ("appDownload", "gmailNewsWrapper", "btn_support")

_KST = timezone(timedelta(hours=9))


class ReaderError(Exception):
    """Safe-to-display validation / extraction problem."""


# ---------------------------------------------------------------- time ----

def to_utc_iso(value: str, naive_tz: timezone = timezone.utc) -> str:
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
    """Best-effort publication time (UTC ISO) from meta, JSON-LD, NAVER or <time>.

    Site-independent on purpose: v3 ran it on every page regardless of the site,
    and a page saved from one site can carry another site's markup.
    """
    meta = soup.select_one('meta[property="article:published_time"], meta[name="article:published_time"]')
    if meta and to_utc_iso(str(meta.get("content", ""))):
        return to_utc_iso(str(meta["content"]))
    for script in soup.find_all("script", type="application/ld+json"):
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string or "")
        if match and to_utc_iso(match.group(1)):
            return to_utc_iso(match.group(1))
    # NAVER prints local Korean time without a UTC offset.
    naver = soup.select_one("span._ARTICLE_DATE_TIME, span.media_end_head_info_datestamp_time")
    if naver:
        stamp = to_utc_iso(str(naver.get("data-date-time", "")), naive_tz=_KST)
        if stamp:
            return stamp
    time_tag = soup.select_one("time[datetime]")
    return to_utc_iso(str(time_tag.get("datetime", ""))) if time_tag else ""


# -------------------------------------------------------------- images ----

def image_source(image: Tag) -> str:
    """The best URL for one <img>: the largest srcset entry up to 1600w, else the smallest, else src."""
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
        usable = [candidate for candidate in candidates if 0 < candidate[0] <= 1600]
        if usable:
            return max(usable)[1]
        return min(candidates)[1]
    return str(image.get("data-src") or image.get("data-original") or image.get("src") or "").strip()


def _is_image_url(source: str) -> bool:
    parsed = urlsplit(source)
    return parsed.scheme in {"http", "https"} and Path(parsed.path).suffix.lower() in IMAGE_EXTENSIONS


def collect_images(page: "Page", selector: str, title: str) -> list[dict]:
    """Article images (max 8, de-duplicated); falls back to og:image / twitter:image."""
    images: list[dict] = []
    seen: set[str] = set()
    for image in page.soup.select(selector):
        if image.find_parent(class_=_CHROME_IMAGE_PARENTS):
            continue
        # <template> content is never rendered: BBC video pages keep an alt-less placeholder
        # image there (O-03, decided for Phase 8; the one known difference from v3).
        if image.find_parent("template") is not None:
            continue
        source = image_source(image)
        if not source:
            continue
        source = urljoin(page.url, source)
        parsed = urlsplit(source)
        if parsed.scheme not in {"http", "https"}:
            continue
        if "/website/img/" in parsed.path:  # CNA site icons and logos
            continue
        if Path(parsed.path).suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if source in seen:
            continue
        seen.add(source)
        images.append({"src": source, "alt": str(image.get("alt", "")).strip()})
        if len(images) >= MAX_IMAGES:
            break
    if not images:
        og_image = page.soup.select_one('meta[property="og:image"]') or page.soup.select_one('meta[name="twitter:image"]')
        if og_image:
            source = urljoin(page.url, str(og_image.get("content", "")).strip())
            if _is_image_url(source):
                images.append({"src": source, "alt": title})
    return images


# ---------------------------------------------------------------- page ----

@dataclass(frozen=True)
class Page:
    """One saved page, parsed once and shared by detection and parsing.

    ``url`` is the canonical link, else ``og:url``, else "". ``host`` is its
    lower-cased hostname. Both come from the page itself, so they are untrusted.
    """

    soup: BeautifulSoup
    url: str
    host: str

    @classmethod
    def from_html(cls, html: bytes | str) -> "Page":
        soup = BeautifulSoup(html, "html.parser")
        canonical = soup.select_one('link[rel="canonical"]')
        og_url = soup.select_one('meta[property="og:url"]')
        # v3 tested these Tags for truthiness, not ``is not None``. bs4 4.15 defines
        # Tag.__bool__ as always True so the two agree, but keeping the v3 form means
        # equivalence holds even on a bs4 release where an empty Tag is falsy.
        url = str(canonical.get("href", "")) if canonical else ""
        if not url and og_url:
            url = str(og_url.get("content", ""))
        return cls(soup=soup, url=url, host=(urlsplit(url).hostname or "").lower())


@dataclass(frozen=True)
class SiteParse:
    """What a site adapter found. ``title_tag`` may be None: the shared fallback then applies."""

    kind: str
    page_type: str
    title_tag: Tag | None
    paragraphs: list[str]
    image_selector: str
    #: Canonical URLs on other hosts are dropped (the page may claim anything).
    url_hosts: frozenset[str]


def resolve_title(soup: BeautifulSoup, title_tag: Tag | None) -> str:
    """The heading text, else og:title, else <title>, else a fixed placeholder."""
    fallback = soup.select_one('meta[property="og:title"]') or soup.select_one("title")
    if title_tag:  # truthiness kept from v3 (see Page.from_html)
        return title_tag.get_text(" ", strip=True)
    if fallback is None:
        return UNKNOWN_TITLE
    if fallback.name == "meta":
        return str(fallback.get("content", UNKNOWN_TITLE))
    return fallback.get_text(" ", strip=True)


def build_article(page: Page, parsed: SiteParse) -> dict:
    """Turn an adapter's findings into the article dictionary (the app-wide contract)."""
    title = resolve_title(page.soup, parsed.title_tag)
    if not parsed.paragraphs:
        raise ReaderError(NO_BODY_MESSAGE)
    images = collect_images(page, parsed.image_selector, title)
    url = page.url if page.host in parsed.url_hosts and page.url.startswith("https://") else ""
    return {
        "title": title,
        "kind": parsed.kind,
        "page_type": parsed.page_type,
        "paragraphs": parsed.paragraphs,
        "paragraph_count": len(parsed.paragraphs),
        "body": "\n\n".join(parsed.paragraphs),
        "url": url,
        "images": images,
        "published": extract_published(page.soup),
    }
