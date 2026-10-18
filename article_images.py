"""Cache a small set of article images from the supported news sites."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_COUNT = 8
IMAGE_HOSTS = {
    "ichef.bbci.co.uk",
    "imgnews.pstatic.net",
    "mimgnews.pstatic.net",
    "imgcdn.cna.com.tw",
}
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _allowed_host(host: str) -> bool:
    return any(host == allowed or host.endswith("." + allowed) for allowed in IMAGE_HOSTS)


def cache_article_images(file_path: Path, article_url: str, images: list[dict]) -> list[dict]:
    """Download supported article images beside their HTML and return local names."""
    media_dir = file_path.with_name(file_path.stem + "_media")
    cached = []
    for image in images[:MAX_IMAGE_COUNT]:
        source = str(image.get("src", "")).strip()
        parsed = urlsplit(source)
        if parsed.scheme != "https" or not _allowed_host((parsed.hostname or "").lower()):
            continue

        key = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
        try:
            if media_dir.is_symlink():
                continue
            media_dir.mkdir(parents=True, exist_ok=True)
            existing = next(media_dir.glob(key + ".*"), None)
            if existing and existing.is_file() and not existing.is_symlink():
                cached.append({**image, "filename": existing.name})
                continue

            request = Request(source, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
                "Referer": article_url,
            })
            with urlopen(request, timeout=12) as response:
                content_type = response.headers.get_content_type().lower()
                extension = MIME_EXTENSIONS.get(content_type)
                if not extension:
                    cached.append({**image, "url": source})
                    continue
                data = response.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES or not data.startswith((b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8")):
                    cached.append({**image, "url": source})
                    continue
            filename = key + extension
            target = media_dir / filename
            target.write_bytes(data)
            cached.append({**image, "filename": filename})
        except (HTTPError, URLError, OSError, TimeoutError):
            cached.append({**image, "url": source})
            continue
    return cached
