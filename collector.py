"""Entry point for one collection cycle: ``collector.py --once``.

Kept at the project root, and kept thin: the Windows scheduled task
``NewsHtmlReader-HourlyCollector-ef29a11a`` runs
``.venv\\Scripts\\pythonw.exe collector.py --once`` and ``launcher.py`` registers
that exact command line. All behaviour lives in ``newsreader.collect.runner``.
"""

from __future__ import annotations

from newsreader.collect.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
