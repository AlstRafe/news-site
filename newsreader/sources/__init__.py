"""Saved-page parsing: the only layer that knows news-site HTML.

Adding a site = a module with ``NAME``, ``HOSTS``, ``matches(page)`` and
``parse(page)`` (see ``bbc.py``), appended to ``SITES``. Order matters: the
first adapter whose ``matches`` is true parses the page (BBC, then NAVER, then
CNA — the precedence v3 used).
"""
from __future__ import annotations

from newsreader.sources import bbc, cna, naver
from newsreader.sources.base import (
    ALLOWED_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MAX_HTML_BYTES,
    UNSUPPORTED_MESSAGE,
    Page,
    ReaderError,
    SiteParse,
    build_article,
    extract_published,
)

SITES = (bbc, naver, cna)

#: Bump whenever a change to any adapter can change ``extract_article`` output.
#: The index stores the version each row was parsed with and re-parses stale rows
#: in the background, so improved extraction reaches already-indexed articles
#: without anyone deleting the index.
PARSER_VERSION = 1


def detect(page: Page):
    """The adapter module for ``page``, or None when no supported site matches."""
    for site in SITES:
        if site.matches(page):
            return site
    return None


def extract_article(html: bytes | str) -> dict:
    """Title, body paragraphs, images, canonical URL and publication time of a saved page.

    Raises ReaderError (safe to show to the user) for unsupported pages or pages
    without body text.
    """
    page = Page.from_html(html)
    site = detect(page)
    if site is None:
        raise ReaderError(UNSUPPORTED_MESSAGE)
    return build_article(page, site.parse(page))


__all__ = [
    "ALLOWED_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "MAX_HTML_BYTES",
    "PARSER_VERSION",
    "SITES",
    "Page",
    "ReaderError",
    "SiteParse",
    "detect",
    "extract_article",
    "extract_published",
]
