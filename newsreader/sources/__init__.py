"""Saved-page parsing. The only layer that knows HTML.

Phase 2 replaces ``extractor`` with ``base.py`` + one module per site
(``bbc.py`` ``naver.py`` ``cna.py``) and a ``SITES`` registry; adding a site will
then mean implementing ``matches(page)`` / ``parse(page)`` and registering it.
Until then this package re-exports the single-module extractor unchanged, so
callers already import from ``newsreader.sources`` and Phase 2 touches no caller.
"""
from __future__ import annotations

from newsreader.sources.extractor import (  # noqa: F401  (re-exported public API)
    ALLOWED_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MAX_FILE_COUNT,
    MAX_HTML_BYTES,
    ReaderError,
    extract_article,
    extract_published,
    list_html_files,
    list_subdirectories,
    resolve_html_file,
    validate_directory,
)

__all__ = [
    "ALLOWED_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "MAX_FILE_COUNT",
    "MAX_HTML_BYTES",
    "ReaderError",
    "extract_article",
    "extract_published",
    "list_html_files",
    "list_subdirectories",
    "resolve_html_file",
    "validate_directory",
]
