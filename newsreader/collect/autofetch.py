"""Single-cycle, low-volume downloader for supported news article pages."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from newsreader.sources import MAX_HTML_BYTES, ReaderError, extract_article

USER_AGENT = "PersonalNewsReader/1.0"
MAX_NEW_PER_SOURCE_PER_CYCLE = 3
MAX_FEED_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20
NAVER_API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
NAVER_API_RESULT_COUNT = 100
SOURCES = (
    {
        "name": "BBC",
        "directory": "BBC",
        "kind": "rss",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "article_hosts": {"www.bbc.com", "www.bbc.co.uk"},
        "is_article": lambda url: urlsplit(url).path.startswith("/news/"),
    },
    {
        "name": "NAVER NEWS",
        "directory": "NAVER NEWS",
        "kind": "naver-api",
        "url": NAVER_API_URL,
        "article_hosts": {"n.news.naver.com"},
        "is_article": lambda url: bool(re.fullmatch(r"/article/\d+/\d+", urlsplit(url).path)),
    },
    {
        "name": "CNA",
        "directory": "CNA",
        "kind": "rss",
        "url": "https://feeds.feedburner.com/rsscna/intworld",
        "article_hosts": {"www.cna.com.tw", "cna.com.tw"},
        "is_article": lambda url: urlsplit(url).path.startswith("/news/"),
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


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


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
    request = Request(robots_url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            data = response.read(512 * 1024 + 1)
            if len(data) > 512 * 1024:
                raise ValueError("robots.txt is unexpectedly large")
            parser.set_url(robots_url)
            parser.parse(data.decode("utf-8", errors="replace").splitlines())
    except HTTPError as exc:
        # A missing robots file is unavailable (and therefore has no rules);
        # authorization/server failures are treated as disallow to fail closed.
        if exc.code == 404:
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


def _naver_api_links(source: dict, credentials: tuple[str, str], query: str) -> list[str]:
    query_string = urlencode({
        "query": query,
        "display": NAVER_API_RESULT_COUNT,
        "start": 1,
        "sort": "date",
        "format": "json",
    })
    request = Request(
        f"{NAVER_API_URL}?{query_string}",
        headers={
            "X-NCP-APIGW-API-KEY-ID": credentials[0],
            "X-NCP-APIGW-API-KEY": credentials[1],
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        data = response.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("NAVER API HUB response exceeded size limit")
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("NAVER API HUB returned an invalid news response")
    links = []
    seen = set()
    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("link", ""))
        parts = urlsplit(candidate)
        if (parts.hostname or "").lower() == "n.news.naver.com":
            candidate = urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))
        normalized = _normalize_url(candidate)
        if not normalized or normalized in seen:
            continue
        if not _host_allowed(normalized, source["article_hosts"]) or not source["is_article"](normalized):
            continue
        seen.add(normalized)
        links.append(normalized)
    return links


def _article_links(
    source: dict,
    naver_credentials: tuple[str, str] | None = None,
    naver_query: str = "뉴스",
) -> list[str]:
    if source["kind"] == "naver-api":
        if not naver_credentials or not all(naver_credentials):
            _log.warning("NAVER API HUB credentials are not configured; add Client ID and Client Secret in app settings")
            return []
        return _naver_api_links(source, naver_credentials, naver_query)

    result = _download(source["url"], MAX_FEED_BYTES)
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
        normalized = _normalize_url(link)
        if not normalized or normalized in seen:
            continue
        if not _host_allowed(normalized, source["article_hosts"]):
            continue
        if not source["is_article"](normalized):
            continue
        seen.add(normalized)
        accepted.append(normalized)
    return accepted


def _existing_article_urls(directory: Path) -> set[str]:
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
            normalized = _normalize_url(url)
            if normalized:
                existing.add(normalized)
        except OSError:
            continue
    return existing


def _filename_for(source: dict, url: str) -> str:
    parts = urlsplit(url)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", parts.path.strip("/"))[:100].strip("_")
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    return f"{source['directory'].lower().replace(' ', '_')}_{slug or 'article'}_{digest}.html"


def _fetch_article(source: dict, url: str, destination: Path) -> str:
    result = _download(url, MAX_HTML_BYTES)
    if result is None:
        return "skipped"
    data, final_url, content_type = result
    final_url = _normalize_url(final_url)
    if not final_url or not _host_allowed(final_url, source["article_hosts"]) or not source["is_article"](final_url):
        _log.warning("Redirected outside the allowed article host; skipped %s", url)
        return "skipped"
    if content_type not in {"text/html", "application/xhtml+xml"} and not data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        _log.warning("Non-HTML response; skipped %s", url)
        return "skipped"
    try:
        article = extract_article(data)
    except ReaderError as exc:
        _log.info("Unsupported or incomplete article skipped (%s): %s", type(exc).__name__, url)
        return "skipped"
    canonical = _normalize_url(article.get("url", ""))
    if not canonical or not _host_allowed(canonical, source["article_hosts"]):
        _log.warning("Article has no supported canonical URL; skipped %s", url)
        return "skipped"
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / _filename_for(source, canonical)
    if target.exists() or target.is_symlink():
        return "duplicate"
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
    naver_credentials: tuple[str, str] | None = None,
    naver_query: str = "뉴스",
) -> dict[str, int]:
    """Fetch up to a few new articles per source, preserving original HTML."""
    totals = {}
    for source in SOURCES:
        directory = library_root / source["directory"]
        existing = _existing_article_urls(directory)
        saved = 0
        duplicates = 0
        try:
            links = _article_links(source, naver_credentials, naver_query)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            _log.warning("%s discovery failed (%s)", source["name"], type(exc).__name__)
            totals[source["name"]] = 0
            continue

        for link in links:
            if link in existing:
                duplicates += 1
                continue
            try:
                result = _fetch_article(source, link, directory)
            except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
                _log.warning("%s article fetch failed (%s): %s", source["name"], type(exc).__name__, link)
                continue
            if result == "duplicate":
                duplicates += 1
            elif result not in {"skipped", "duplicate"}:
                existing.add(result)
                saved += 1
                _log.info("Saved %s: %s", source["name"], result)
            if saved >= MAX_NEW_PER_SOURCE_PER_CYCLE:
                break
        totals[source["name"]] = saved
        _log.info("%s cycle complete: saved=%d duplicates=%d", source["name"], saved, duplicates)
    return totals


