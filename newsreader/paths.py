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
#: Written by launcher.py once the start-at-logon task is registered (launcher keeps its own copy of the name).
LOGON_TASK_RECORD = INSTANCE_DIR / "server-task.json"
#: Written last by a deployment. A supervised server that sees it change restarts
#: itself into the new code (see web/run.py and launcher.py).
VERSION_PATH = PROJECT_ROOT / "VERSION"
#: A test build's preset (make_alpha_zip.py): settings applied once by launcher.py and a relay
#: token moved into the credential store on first start (services/preset.py). Absent normally.
PRESET_DIR = PROJECT_ROOT / "preset"
PRESET_TOKEN_PATH = PRESET_DIR / "relay_token"


def deployed_version() -> str:
    try:
        return VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

#: Used when ``HTML_DIRECTORY`` is unset. The collector keeps using this default
#: directly (it does not read ``HTML_DIRECTORY``); see ``collect/runner.py``.
DEFAULT_ARTICLES_DIR = PROJECT_ROOT / "articles"


def configured_library_dir() -> str:
    """The "記事フォルダ" saved on the settings page (instance/settings.json), or ""."""
    import json
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("library_dir", "")
    except (OSError, ValueError, TypeError, AttributeError):
        return ""
    return value.strip() if isinstance(value, str) else ""


def articles_dir() -> Path:
    """The saved-HTML library the web app serves.

    ``HTML_DIRECTORY`` wins so one checkout can read a library kept anywhere on
    the PC; then the folder saved on the settings page; then ``articles/`` beside
    the app. Not resolved here, matching the pre-Phase-1 web behaviour.
    """
    configured = os.environ.get("HTML_DIRECTORY", "").strip()
    saved = configured_library_dir()
    # launcher.py versions before the settings-page folder passed the default articles/ in
    # HTML_DIRECTORY explicitly; that is not a choice, so a saved folder still applies.
    if saved and (not configured or _same_path(configured, DEFAULT_ARTICLES_DIR)):
        configured = saved
    # A launcher started before the folder was moved keeps passing the old one on restarts: a
    # folder that no longer exists is no choice either.
    elif saved and configured and not Path(configured).expanduser().exists() and Path(saved).expanduser().exists():
        configured = saved
    return Path(configured).expanduser() if configured else DEFAULT_ARTICLES_DIR


def status_path_beside(library_root: Path) -> Path:
    """Where the installation that owns ``library_root`` keeps its status (the standard layout:
    ``<project>/articles`` next to ``<project>/instance``)."""
    return library_root.parent / INSTANCE_DIR.name / STATUS_PATH.name


def same_path(a: Path | str, b: Path | str) -> bool:
    """Same folder after resolving (and, on Windows, ignoring case)."""
    try:
        return (os.path.normcase(str(Path(a).expanduser().resolve()))
                == os.path.normcase(str(Path(b).expanduser().resolve())))
    except (OSError, RuntimeError):
        return False


def _same_path(text: str, path: Path) -> bool:
    return same_path(text, path)
