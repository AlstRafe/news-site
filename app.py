"""Entry point for the web app.

Kept at the project root, and kept thin: ``launcher.py`` and ``start_windows.bat``
run exactly this path, so moving or renaming it would break the Windows startup
route. Importing it has no side effects; all behaviour lives in ``newsreader.web``.
"""
from __future__ import annotations

from newsreader.web.run import main

if __name__ == "__main__":
    raise SystemExit(main())
