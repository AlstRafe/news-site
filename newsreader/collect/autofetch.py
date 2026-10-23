"""Single-cycle, low-volume downloader for supported news article pages."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from newsreader.sources import MAX_HTML_BYTES, ReaderError, extract_article
from newsreader.sources.slim import slim_if_equivalent

USER_AGENT = "PersonalNewsReader/1.0"
MAX_NEW_PER_SOURCE_PER_CYCLE = 3
#: Each outlet's section feeds (経済, テクノロジー ...) add at most this many per feed and cycle, after
#: its main (international) feed, so the kinds of news the lists can show are not world news only.
MAX_NEW_PER_SECTION_FEED = 2
TECH_PER_CYCLE = 5


#: A page that could not be read as an article (a BBC video page without text, for example) is
#: not downloaded again for this long: the same few pages used to be fetched every hour.
SKIP_REMEMBER_SECONDS = 24 * 3600


class SkipMemory:
    """URLs recently skipped as unreadable, kept in a small JSON file beside the index.

    Only "the page is not a readable article" is remembered — never network failures, which
    are retried next hour. A missing or corrupt file simply means nothing is remembered.
    """

    def __init__(self, path: Path | None, now: float | None = None):
        self.path, self.now = path, time.time() if now is None else now
        self.entries: dict[str, float] = {}
        if path is not None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.entries = {str(url): float(at) for url, at in data.items()
                                if self.now - float(at) < SKIP_REMEMBER_SECONDS}
            except (OSError, ValueError, TypeError, AttributeError):
                self.entries = {}

    def __contains__(self, url: str) -> bool:
        return url in self.entries

    def add(self, url: str) -> None:
        self.entries[url] = self.now

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.entries, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            _log.warning("Could not save the list of skipped pages")


def slim_on_save() -> bool:
    """Save new pages slimmed (D-11; only when extraction is identical). NEWS_SLIM_ON_SAVE=0 keeps originals."""
    return os.environ.get("NEWS_SLIM_ON_SAVE", "1").strip() not in {"0", "false", "no", "off"}
MAX_FEED_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20
SOURCES = (
    {
        "name": "BBC",
        "directory": "BBC",
        "kind": "rss",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "section_feeds": ("https://feeds.bbci.co.uk/news/business/rss.xml",
                          "https://feeds.bbci.co.uk/news/technology/rss.xml",
                          "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
        "article_hosts": {"www.bbc.com", "www.bbc.co.uk"},
        "is_article": lambda url: urlsplit(url).path.startswith("/news/"),
    },
    {
        "name": "CNA",
        "directory": "CNA",
        "kind": "rss",
        "url": "https://feeds.feedburner.com/rsscna/intworld",
        "section_feeds": ("https://feeds.feedburner.com/rsscna/finance",
                          "https://feeds.feedburner.com/rsscna/technology"),
        "article_hosts": {"www.cna.com.tw", "cna.com.tw"},
        "is_article": lambda url: urlsplit(url).path.startswith("/news/"),
    },
    {
        # 연합뉴스 international news (replaces NAVER, whose API needs keys that cannot be had).
        "name": "Yonhap",
        "directory": "Yonhap",
        "kind": "rss",
        "url": "https://www.yna.co.kr/rss/international.xml",
        "section_feeds": ("https://www.yna.co.kr/rss/economy.xml",
                          "https://www.yna.co.kr/rss/industry.xml"),
        "article_hosts": {"www.yna.co.kr"},
        "is_article": lambda url: urlsplit(url).path.startswith("/view/AKR"),
    },
    # Technology outlets (networks, programming, security), read by sources/generic.py; the
    # reader asked for more of these, so each may add TECH_PER_CYCLE articles an hour. Saved
    # unslimmed: a later, better generic reading needs the whole page.
    {
        "name": "InfoQ",
        "directory": "InfoQ",
        "per_cycle": TECH_PER_CYCLE,
        "slim": False,
        "kind": "rss",
        "url": "https://feed.infoq.com/",
        "article_hosts": {"www.infoq.com"},
        "is_article": lambda url: re.match(r"/news/\d{4}/\d{2}/[^/]+$", urlsplit(url).path) is not None,
    },
    {
        "name": "The New Stack",
        "directory": "The New Stack",
        "per_cycle": TECH_PER_CYCLE,
        "slim": False,
        "kind": "rss",
        "url": "https://thenewstack.io/feed/",
        "article_hosts": {"thenewstack.io"},
        "is_article": lambda url: re.match(r"/[a-z0-9][a-z0-9-]{8,}$", urlsplit(url).path) is not None,
    },
    {
        "name": "iThome",
        "directory": "iThome",
        "per_cycle": TECH_PER_CYCLE,
        "slim": False,
        "kind": "rss",
        "url": "https://www.ithome.com.tw/rss",
        "article_hosts": {"www.ithome.com.tw"},
        "is_article": lambda url: re.match(r"/news/\d+$", urlsplit(url).path) is not None,
    },
    {
        "name": "ZDNet Korea",
        "directory": "ZDNet Korea",
        "per_cycle": TECH_PER_CYCLE,
        "slim": False,
        "kind": "rss",
        "url": "https://feeds.feedburner.com/zdkorea",
        "article_hosts": {"zdnet.co.kr"},
        # Articles are /view/?no=<number>: that one query parameter is the article.
        "keep_query": ("no",),
        # Personnel moves and obituaries are notices, not news.
        "skip_title": r"^\s*\[(?:인사|부고|동정|알림)\]",
        "is_article": lambda url: urlsplit(url).path == "/view" and re.search(r"(?:^|&)no=\d+", urlsplit(url).query) is not None,
    },
)

_log = logging.getLogger("news_autofetch")
_robots_cache: dict[str, tuple[float, RobotFileParser | None]] = {}
_robots_lock = threading.Lock()
def configure_logging(log_directory: Path) -> None:
    _log.setLevel(logging.INFO)
    if _log.handlers:
        return
    log_directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_directory / "news_autofetch.log", maxBytes=1_000_000,
        backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _log.addHandler(handler)


def _normalize_url(url: str, keep: tuple[str, ...] = ()) -> str:
    """Scheme and host lower-cased, no trailing slash, no fragment; the query is dropped except
    the parameters a site's addresses depend on (``keep``)."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    path = parts.path.rstrip("/") or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if k in keep)) if keep else ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def _host_allowed(url: str, allowed_hosts: set[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in allowed_hosts


def _fetch_robots(origin: str) -> RobotFileParser | None:
    now = time.monotonic()
    with _robots_lock:
        cached = _robots_cache.get(origin)
        if cached and now - cached[0] < 24 * 60 * 60:
            return cached[1]

    parser = RobotFileParser()
    robots_url = origin.rstrip("/") + "/robots.txt"
    request = Request(robots_url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain, */*;q=0.8"})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            data = response.read(512 * 1024 + 1)
            if len(data) > 512 * 1024:
                raise ValueError("robots.txt is unexpectedly large")
            parser.set_url(robots_url)
            parser.parse(data.decode("utf-8", errors="replace").splitlines())
    except HTTPError as exc:
        # RFC 9309: a robots file that is unavailable (4xx) sets no rules; refused access
        # (401/403), rate limiting (429) and server failures are treated as disallow to fail closed.
        if 400 <= exc.code < 500 and exc.code not in (401, 403, 429):
            parser.set_url(robots_url)
            parser.parse([])
        else:
            _log.warning("robots.txt unavailable for %s (HTTP %s); skipping host", origin, exc.code)
            parser = None
    except (URLError, OSError, TimeoutError, ValueError) as exc:
        _log.warning("robots.txt unavailable for %s (%s); skipping host", origin, type(exc).__name__)
        parser = None

    with _robots_lock:
        _robots_cache[origin] = (now, parser)
    return parser


def _allowed_by_robots(url: str) -> bool:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    parser = _fetch_robots(origin)
    if parser is None:
        return False
    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        _log.warning("Could not evaluate robots.txt for %s; skipping URL", url)
        return False


def _download(url: str, max_bytes: int) -> tuple[bytes, str, str] | None:
    if not _allowed_by_robots(url):
        _log.info("robots.txt disallows or could not verify %s", url)
        return None
    request = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, text/html, application/xml;q=0.9, */*;q=0.5",
    })
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        final_url = response.geturl()
        content_type = response.headers.get_content_type().lower()
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        _log.warning("Response exceeded size limit; skipped %s", url)
        return None
    return data, final_url, content_type


def _article_links(source: dict, feed: str = "") -> list[str]:
    result = _download(feed or source["url"], MAX_FEED_BYTES)
    if result is None:
        return []
    data, final_url, _ = result
    if source["kind"] == "rss":
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise ValueError(f"invalid RSS/XML ({type(exc).__name__})") from None
        links = []
        for item in root.iter():
            if item.tag.rsplit("}", 1)[-1].lower() not in {"item", "entry"}:
                continue
            link = ""
            for child in item:
                if child.tag.rsplit("}", 1)[-1].lower() == "link":
                    link = child.attrib.get("href", "") or (child.text or "").strip()
                    if link:
                        break
            if link:
                links.append(urljoin(final_url, link))
    else:
        soup = BeautifulSoup(data, "html.parser")
        links = [urljoin(final_url, str(anchor.get("href", ""))) for anchor in soup.select("a[href]")]

    accepted = []
    seen = set()
    for link in links:
        normalized = _normalize_url(link, source.get("keep_query", ()))
        if not normalized or normalized in seen:
            continue
        if not _host_allowed(normalized, source["article_hosts"]):
            continue
        if not source["is_article"](normalized):
            continue
        seen.add(normalized)
        accepted.append(normalized)
    return accepted


def _existing_article_urls(directory: Path, keep: tuple[str, ...] = ()) -> set[str]:
    existing = set()
    if not directory.is_dir():
        return existing
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {".html", ".htm"}:
            continue
        try:
            with path.open("rb") as html_file:
                soup = BeautifulSoup(html_file.read(256 * 1024), "html.parser")
            canonical = soup.select_one('link[rel="canonical"]')
            og_url = soup.select_one('meta[property="og:url"]')
            url = str(canonical.get("href", "")) if canonical else ""
            if not url and og_url:
                url = str(og_url.get("content", ""))
            normalized = _normalize_url(url, keep)
            if normalized:
                existing.add(normalized)
        except OSError:
            continue
    return existing


def _filename_for(source: dict, url: str) -> str:
    parts = urlsplit(url)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", parts.path.strip("/") + ("_" + parts.query if parts.query else ""))[:100].strip("_")
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    return f"{source['directory'].lower().replace(' ', '_')}_{slug or 'article'}_{digest}.html"


def _fetch_article(source: dict, url: str, destination: Path) -> str:
    """Canonical URL when saved; "duplicate"; "skipped" (robots, size: try again next time);
    "unreadable" (not a readable article: remembered for a day, see SkipMemory)."""
    result = _download(url, MAX_HTML_BYTES)
    if result is None:
        return "skipped"
    data, final_url, content_type = result
    keep = source.get("keep_query", ())
    final_url = _normalize_url(final_url, keep)
    if not final_url or not _host_allowed(final_url, source["article_hosts"]) or not source["is_article"](final_url):
        _log.warning("Redirected outside the allowed article host; skipped %s", url)
        return "unreadable"
    if content_type not in {"text/html", "application/xhtml+xml"} and not data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        _log.warning("Non-HTML response; skipped %s", url)
        return "unreadable"
    try:
        article = extract_article(data)
    except ReaderError as exc:
        _log.info("Unsupported or incomplete article skipped (%s): %s", type(exc).__name__, url)
        return "unreadable"
    skip_title = source.get("skip_title")
    if skip_title and re.search(skip_title, article.get("title", "")):
        _log.info("Not news (%s); skipped %s", article.get("title", "")[:40], url)
        return "unreadable"
    canonical = _normalize_url(article.get("url", ""), keep)
    if not canonical or not _host_allowed(canonical, source["article_hosts"]):
        _log.warning("Article has no supported canonical URL; skipped %s", url)
        return "unreadable"
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / _filename_for(source, canonical)
    if target.exists() or target.is_symlink():
        return "duplicate"
    if slim_on_save() and source.get("slim", True):
        kept, slimmed = slim_if_equivalent(data, article)
        if slimmed:
            _log.info("Slimmed before saving: %d -> %d bytes (extraction identical)", len(data), len(kept))
        data = kept
    temporary = target.with_suffix(".html.part")
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return canonical


def fetch_cycle(
    library_root: Path,
    report: dict | None = None,
    skip_memory_path: Path | None = None,
    limit: int = 0,
) -> dict[str, int]:
    """Fetch up to a few new articles per source, preserving original HTML.

    Returns {site: saved} exactly as before. When ``report`` is given it is filled with a
    per-site breakdown for the status page: saved / duplicates / skipped / failed, and the
    reason when a site could not be checked at all.
    """
    totals = {}
    remembered = SkipMemory(skip_memory_path)
    # With a limit (test builds), stop adding once the library holds that many articles.
    held = sum(1 for _ in library_root.rglob("*.html")) if limit and library_root.is_dir() else 0
    for source in SOURCES:
        directory = library_root / source["directory"]
        saved = duplicates = skipped = failed = 0
        site = {"saved": 0, "duplicates": 0, "skipped": 0, "failed": 0, "error": ""}
        if report is not None:
            report[source["name"]] = site
        if limit and held >= limit:
            site["error"] = f"記事数の上限（{limit} 本）に達しているため取得していません"
            site["limited"] = True
            totals[source["name"]] = 0
            continue
        existing = _existing_article_urls(directory, source.get("keep_query", ()))
        try:
            links = _article_links(source)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            _log.warning("%s discovery failed (%s)", source["name"], type(exc).__name__)
            site["error"] = f"一覧の取得に失敗（{type(exc).__name__}）"
            totals[source["name"]] = 0
            continue
        # The main feed first, then each section feed with its own small allowance. A section
        # feed that cannot be read only costs its own articles this cycle.
        queue = [(link, 0) for link in links]
        for number, feed in enumerate(source.get("section_feeds", ()), start=1):
            try:
                queue += [(link, number) for link in _article_links(source, feed)]
            except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
                _log.warning("%s section feed failed (%s): %s", source["name"], type(exc).__name__, feed)
        saved_from: dict[int, int] = {}
        queued: set[str] = set()

        for link, feed_number in queue:
            if link in queued:
                continue
            queued.add(link)
            allowance = MAX_NEW_PER_SECTION_FEED if feed_number else source.get("per_cycle", MAX_NEW_PER_SOURCE_PER_CYCLE)
            if saved_from.get(feed_number, 0) >= allowance:
                continue
            if link in existing:
                duplicates += 1
                continue
            if link in remembered:  # unreadable last time: do not download it again yet
                skipped += 1
                continue
            try:
                result = _fetch_article(source, link, directory)
            except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
                _log.warning("%s article fetch failed (%s): %s", source["name"], type(exc).__name__, link)
                failed += 1
                continue
            if result == "duplicate":
                duplicates += 1
            elif result in ("skipped", "unreadable"):
                skipped += 1
                if result == "unreadable":
                    remembered.add(link)
            else:
                existing.add(result)
                saved += 1
                saved_from[feed_number] = saved_from.get(feed_number, 0) + 1
                held += 1
                _log.info("Saved %s: %s", source["name"], result)
            if limit and held >= limit:
                break
        totals[source["name"]] = saved
        site.update(saved=saved, duplicates=duplicates, skipped=skipped, failed=failed)
        _log.info("%s cycle complete: saved=%d duplicates=%d", source["name"], saved, duplicates)
    remembered.save()
    return totals


