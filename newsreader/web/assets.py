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

from newsreader import paths

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
    """Static files read once, hashed once and gzipped once per process (re-read if the file changes)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._entries: dict[str, StaticEntry] = {}
        self._lock = threading.Lock()

    def get(self, filename: str) -> StaticEntry | None:
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


def localtime_filter(iso_utc: str) -> str:
    """Render a stored UTC timestamp in the server's own timezone (no hard-coded zone)."""
    try:
        moment = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ""


def init_app(app: Flask, *, dev: bool) -> None:
    static = StaticFiles(paths.STATIC_DIR)
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
