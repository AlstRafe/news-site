"""Entry point for the web app.

Kept at the project root, and kept thin: ``launcher.py`` and ``start_windows.bat``
run exactly this path, so moving or renaming it would break the Windows startup
route. All behaviour lives in ``newsreader.web``.
"""
from __future__ import annotations

from newsreader.web.app import app, main  # noqa: F401  ("app" is imported by WSGI callers)

if __name__ == "__main__":
    raise SystemExit(main())
