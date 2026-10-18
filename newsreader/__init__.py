"""News HTML Reader — reads saved BBC / NAVER NEWS / CNA pages with translation tools.

Layer order (never reversed): ``web`` -> ``library`` / ``services`` -> ``sources``.
``paths`` sits below everything and is the only place filesystem locations live.
The root ``app.py`` / ``collector.py`` stay thin so ``launcher.py`` and the
Windows scheduled task keep working unchanged.

``create_app()`` arrives in Phase 4 together with the ``web`` split; until then
``newsreader.web.app`` holds the Flask application object.
"""
from __future__ import annotations

__all__ = ["paths"]
