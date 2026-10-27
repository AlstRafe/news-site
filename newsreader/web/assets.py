"""HTTP caching and compression: the part of "fast page transitions" that is not the index (D-06).

- Static files: URL carries a content hash (``?v=``) so they can be cached for a year
  (``immutable``); served from a small in-memory cache, pre-gzipped.
- HTML: weak ETag + 304, gzip. Article pages may be reused for 60 s (``private``) so a
  prefetched page opens instantly; the list revalidates every time (``no-cache``).
- JSON API: never cached (``no-store``); large answers (whole-article translations) gzipped.
"""
from __future__ import annotations

import gzip
import hashlib
import mimetypes
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, abort, request, url_for
from werkzeug.security import safe_join

from newsreader import i18n, paths

YEAR = 365 * 24 * 3600
MIN_GZIP_BYTES = 1024
_COMPRESSIBLE = {"text/html", "text/css", "application/javascript", "text/javascript", "application/json",
                 "text/plain", "image/svg+xml", "application/manifest+json"}

CACHE_LIST = "no-cache"
CACHE_PAGE = "private, max-age=60"
CACHE_API = "no-store"


@dataclass(frozen=True)
class StaticEntry:
    key: tuple[int, int]
    data: bytes
    gz: bytes | None
    digest: str
    mimetype: str


class StaticFiles:
    """Static files read once, hashed once and gzipped once per process.

    ``frozen`` (production): every file is loaded at start-up and never re-read, so a running
    server always serves the CSS/JS that match the templates it compiled at start-up — a
    deployment that replaces files under it cannot mix old pages with new scripts. The new
    version arrives with the restart that the deployment triggers (VERSION, web/run.py).
    Not frozen (NEWS_DEV=1, tests): re-read when a file changes.
    """

    def __init__(self, root: Path, frozen: bool = False):
        self.root = Path(root)
        self._entries: dict[str, StaticEntry] = {}
        self._lock = threading.Lock()
        self.frozen = False
        if frozen:
            for path in sorted(self.root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    self.get(path.relative_to(self.root).as_posix())
            self.frozen = True

    def get(self, filename: str) -> StaticEntry | None:
        if self.frozen:
            return self._entries.get(filename)
        joined = safe_join(str(self.root), filename)
        if joined is None:
            return None
        path = Path(joined)
        try:
            if path.is_symlink():
                return None
            stat = path.stat()
        except OSError:
            return None
        if not path.is_file():
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        entry = self._entries.get(filename)
        if entry is not None and entry.key == key:
            return entry
        with self._lock:
            data = path.read_bytes()
            mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if mimetype.startswith("text/") or mimetype == "application/javascript":
                mimetype_header = mimetype + "; charset=utf-8"
            else:
                mimetype_header = mimetype
            gz = None
            if mimetype in _COMPRESSIBLE and len(data) >= MIN_GZIP_BYTES:
                packed = gzip.compress(data, compresslevel=9, mtime=0)
                gz = packed if len(packed) < len(data) else None
            entry = StaticEntry(key, data, gz, hashlib.sha256(data).hexdigest()[:12], mimetype_header)
            self._entries[filename] = entry
            return entry


def accepts_gzip() -> bool:
    return request.accept_encodings["gzip"] > 0


def _local(iso_utc: str) -> datetime | None:
    """A stored UTC timestamp in the server's own timezone (no hard-coded zone)."""
    try:
        return datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()
    except (ValueError, TypeError):
        return None


def localtime_filter(iso_utc: str) -> str:
    """The date and time the way the page's language writes them: "2026年10月23日（金）16:20"."""
    moment = _local(iso_utc)
    return i18n.date_time(moment) if moment else ""


def lag_filter(iso_utc: str, first_iso: str) -> str:
    """How long after the first report of a news group an article came out: "+45分", "+3時間10分",
    "+2日"; "" for the first one itself or without both times."""
    try:
        moment = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ")
        first = datetime.strptime(first_iso, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ""
    minutes = int((moment - first).total_seconds() // 60)
    return "+" + i18n.minutes(minutes) if minutes >= 1 else ""


def localtime_short_filter(iso_utc: str) -> str:
    """"10/13 21:00" in the server's timezone (lists where the year is obvious)."""
    moment = _local(iso_utc)
    return i18n.short_time(moment) if moment else ""


def localclock_filter(iso_utc: str) -> str:
    """"21:00" in the server's timezone (the day is shown by the list's day separators)."""
    moment = _local(iso_utc)
    return moment.strftime("%H:%M") if moment else ""


def localday_filter(iso_utc: str, today=None) -> str:
    """The local day of a UTC timestamp for list separators: "今日 · 10月19日（月）"."""
    moment = _local(iso_utc)
    if moment is None:
        return ""
    days = ((today or datetime.now().astimezone()).date() - moment.date()).days
    near = {0: i18n.text("今日"), 1: i18n.text("昨日")}.get(days)
    return f"{near} · {i18n.day(moment)}" if near else i18n.day(moment)


def init_app(app: Flask, *, dev: bool) -> None:
    static = StaticFiles(paths.STATIC_DIR, frozen=not dev)
    app.extensions["static_files"] = static
    # Templates are compiled once per process unless NEWS_DEV=1 (§6): no per-request stat of
    # every template file.
    app.config["TEMPLATES_AUTO_RELOAD"] = dev
    app.jinja_env.auto_reload = dev

    def serve_static(filename: str):
        entry = static.get(filename)
        if entry is None:
            abort(404)
        use_gzip = entry.gz is not None and accepts_gzip()
        response = Response(entry.gz if use_gzip else entry.data, mimetype=None)
        response.headers["Content-Type"] = entry.mimetype
        response.set_etag(entry.digest, weak=True)
        if use_gzip:
            response.headers["Content-Encoding"] = "gzip"
        response.headers["Vary"] = "Accept-Encoding"
        # Only a URL that names this exact content may be cached forever.
        response.headers["Cache-Control"] = (f"public, max-age={YEAR}, immutable"
                                             if request.args.get("v") == entry.digest else "no-cache")
        response.make_conditional(request)
        return response

    app.add_url_rule("/static/<path:filename>", endpoint="static", view_func=serve_static)

    def asset(filename: str) -> str:
        entry = static.get(filename)
        return url_for("static", filename=filename, v=entry.digest) if entry else url_for("static", filename=filename)

    app.jinja_env.globals["asset"] = asset
    app.add_template_filter(localtime_filter, "localtime")
    app.add_template_filter(localtime_short_filter, "localtime_short")
    app.add_template_filter(lag_filter, "lag")
    app.add_template_filter(localday_filter, "localday")
    app.add_template_filter(localclock_filter, "localclock")

    @app.after_request
    def _cache_and_compress(response: Response):
        if request.endpoint == "static" or response.direct_passthrough or response.is_streamed:
            return response
        mimetype = response.mimetype or ""
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = CACHE_API
        elif mimetype == "text/html":
            if response.status_code == 200:
                response.headers.setdefault("Cache-Control", CACHE_LIST)
                response.add_etag(weak=True)
                response.make_conditional(request)
            else:
                response.headers.setdefault("Cache-Control", "no-store")
        if (response.status_code == 200 and mimetype in _COMPRESSIBLE and "Content-Encoding" not in response.headers
                and accepts_gzip()):
            body = response.get_data()
            if len(body) >= MIN_GZIP_BYTES:
                response.set_data(gzip.compress(body, compresslevel=6, mtime=0))
                response.headers["Content-Encoding"] = "gzip"
        if mimetype in _COMPRESSIBLE:
            response.vary.add("Accept-Encoding")
        return response


def is_dev() -> bool:
    return os.environ.get("NEWS_DEV", "").strip() == "1"
