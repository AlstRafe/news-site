"""The SQLite index over the saved HTML library.

``index`` is re-exported here so callers write ``from newsreader.library import index``
and Phase 3 can add ``files.py`` / ``stories.py`` / ``status.py`` / ``pipeline.py``
beside it without touching callers.
"""

from __future__ import annotations

from newsreader.library import index  # noqa: F401

__all__ = ["index"]
