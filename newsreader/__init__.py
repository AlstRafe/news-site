"""News HTML Reader — reads saved BBC / NAVER NEWS / CNA pages with translation tools.

Layer order (never reversed): ``web`` -> ``library`` / ``services`` -> ``sources``.
``paths`` sits below everything and is the only place filesystem locations live.
The root ``app.py`` / ``collector.py`` stay thin so ``launcher.py`` and the
Windows scheduled task keep working unchanged.

``create_app()`` builds the web application (see ``newsreader.web``). It is imported
lazily so that the collector, which never serves HTTP, does not pay for importing Flask.
"""
from __future__ import annotations

__all__ = ["create_app", "paths"]


def create_app(*args, **kwargs):
    from newsreader.web import create_app as _create_app

    return _create_app(*args, **kwargs)
