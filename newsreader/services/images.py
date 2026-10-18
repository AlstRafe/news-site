"""Article images on demand (D-05).

Rendering a page never downloads anything: the page points at ``/img/<id>/<n>`` and the
first request for that URL fetches the image from the news site's image host, stores it
beside the article (``<article>_media/<hash>.<ext>``, the v3 cache layout, so existing
caches keep working) and every later request is a plain file read.

Only https URLs on the four known image hosts are fetched, redirects may not leave those
hosts, responses are size-capped and must really be images (magic bytes), so a crafted
saved page cannot turn the app into a fetcher of arbitrary URLs.
"""
from __future__ import annotations

import hashlib
import os
import ssl
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import truststore

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_COUNT = 8
FETCH_TIMEOUT_SECONDS = 8
#: A failed fetch (offline, 404, not an image) is not retried for this long, so a page full
#: of unavailable thumbnails does not keep worker threads waiting on the network.
FAILURE_TTL_SECONDS = 10 * 60
#: Upper bound on simultaneous downloads; the rest wait instead of piling up on the network.
MAX_PARALLEL_FETCHES = 6

IMAGE_HOSTS = frozenset({
    "ichef.bbci.co.uk",
    "imgnews.pstatic.net",
    "mimgnews.pstatic.net",
    "imgcdn.cna.com.tw",
})
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
EXTENSION_MIMES = {ext: mime for mime, ext in MIME_EXTENSIONS.items()} | {".jpeg": "image/jpeg"}
_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8")
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"


class ImageUnavailable(Exception):
    """The image cannot be served (not allowed, not reachable, or not an image)."""


def allowed_host(host: str) -> bool:
    return any(host == allowed or host.endswith("." + allowed) for allowed in IMAGE_HOSTS)


def allowed_image_url(source: str) -> bool:
    parsed = urlsplit(source)
    return parsed.scheme == "https" and allowed_host((parsed.hostname or "").lower())


def image_key(source: str) -> str:
    """The cache file stem for an image URL (same scheme as v3's cache)."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def media_dir_for(article_path: Path) -> Path:
    return article_path.with_name(article_path.stem + "_media")


def find_cached(media_dir: Path, key: str) -> Path | None:
    try:
        if media_dir.is_symlink() or not media_dir.is_dir():
            return None
        for ext in EXTENSION_MIMES:
            candidate = media_dir / (key + ext)
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
    except OSError:
        return None
    return None


class _StayOnImageHosts(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_image_url(newurl):
            raise ImageUnavailable("redirect left the allowed image hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(
    _StayOnImageHosts,
    urllib.request.HTTPSHandler(context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)),
)
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()
_failures: dict[str, float] = {}
_fetch_slots = threading.BoundedSemaphore(MAX_PARALLEL_FETCHES)


def _lock_for(key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def _download(source: str, referer: str) -> tuple[bytes, str]:
    request = urllib.request.Request(source, headers={"User-Agent": _USER_AGENT, "Referer": referer})
    with _opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        if not allowed_image_url(response.geturl()):
            raise ImageUnavailable("final URL is not on an allowed image host")
        extension = MIME_EXTENSIONS.get(response.headers.get_content_type().lower())
        if not extension:
            raise ImageUnavailable("not an image content type")
        data = response.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES or not data.startswith(_MAGIC):
        raise ImageUnavailable("too large or not an image")
    return data, extension


def cached_or_fetch(source: str, media_dir: Path, referer: str = "") -> Path:
    """The local file for ``source``, downloading it once if needed. Raises ImageUnavailable."""
    if not allowed_image_url(source):
        raise ImageUnavailable("not an allowed image URL")
    key = image_key(source)
    cached = find_cached(media_dir, key)
    if cached:
        return cached
    if _failures.get(key, 0) > time.monotonic():
        raise ImageUnavailable("recently failed")
    with _lock_for(key):  # concurrent requests for one image download it once
        cached = find_cached(media_dir, key)
        if cached:
            return cached
        if _failures.get(key, 0) > time.monotonic():
            raise ImageUnavailable("recently failed")
        try:
            with _fetch_slots:
                data, extension = _download(source, referer)
            if media_dir.is_symlink():
                raise ImageUnavailable("media folder is a symlink")
            media_dir.mkdir(parents=True, exist_ok=True)
            target = media_dir / (key + extension)
            fd, temporary = tempfile.mkstemp(prefix=".img-", dir=media_dir)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(temporary, target)  # readers never see a half-written image
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            _failures.pop(key, None)
            return target
        except (ImageUnavailable, HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            _failures[key] = time.monotonic() + FAILURE_TTL_SECONDS
            raise ImageUnavailable(type(exc).__name__) from None
