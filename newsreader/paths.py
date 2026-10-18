"""All filesystem locations in one place.

Why: before Phase 1 every module computed its own ``Path(__file__).parent / "instance"``.
That breaks the moment a module moves to a different directory depth, and it made
per-PC values (article folder, instance folder) impossible to see at a glance.
Nothing outside this module may hard-code a path; per-PC values come from the
environment (``HTML_DIRECTORY``) or from local settings files.
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
#: The folder holding ``app.py`` / ``collector.py``. ``launcher.py`` and
#: ``start_windows.bat`` depend on those entry points staying there.
PROJECT_ROOT = PACKAGE_ROOT.parent

TEMPLATES_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"

INSTANCE_DIR = PROJECT_ROOT / "instance"
DEFAULT_LIBRARY_DB = INSTANCE_DIR / "library.sqlite3"
DEFAULT_TRANSLATION_CACHE = INSTANCE_DIR / "translation_cache.sqlite3"
SETTINGS_PATH = INSTANCE_DIR / "settings.json"
STATUS_PATH = INSTANCE_DIR / "status.json"
SECRET_KEY_PATH = INSTANCE_DIR / "secret_key"
COLLECTOR_SETTINGS_PATH = INSTANCE_DIR / "collector.json"
COLLECTOR_LOCK_PATH = INSTANCE_DIR / "collector.lock"

#: Used when ``HTML_DIRECTORY`` is unset. The collector keeps using this default
#: directly (it does not read ``HTML_DIRECTORY``); see ``collect/runner.py``.
DEFAULT_ARTICLES_DIR = PROJECT_ROOT / "articles"


def articles_dir() -> Path:
    """The saved-HTML library the web app serves.

    ``HTML_DIRECTORY`` wins so one checkout can read a library kept anywhere on
    the PC. Not resolved here, matching the pre-Phase-1 web behaviour.
    """
    configured = os.environ.get("HTML_DIRECTORY", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_ARTICLES_DIR
