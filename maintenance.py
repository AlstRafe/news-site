"""Library maintenance: ``python maintenance.py report|slim|prune|compact [--apply]``.

Dry run unless ``--apply``. See newsreader/library/maintenance.py for what each command does.
"""
from __future__ import annotations

from newsreader.library.maintenance import main

if __name__ == "__main__":
    raise SystemExit(main())
