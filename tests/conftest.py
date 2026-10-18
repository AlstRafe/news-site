"""Every test runs against throw-away instance files.

CLAUDE.md §5: tests must never write to the real instance/ (translation cache, settings,
secret key, index, status). Modules keep their paths as module attributes; this session
fixture points all of them at a temporary directory before any test runs, and the
test-suite-level check at the end proves nothing was created in the project's instance/.
"""
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def isolated_instance():
    from newsreader import paths
    from newsreader.library import index, status
    from newsreader.services import cache, settings

    instance = ROOT / "instance"
    before = sorted(p.name for p in instance.iterdir()) if instance.is_dir() else None
    tmp = Path(tempfile.mkdtemp(prefix="newsreader-tests-"))
    saved = (cache.CACHE_PATH, settings.SETTINGS_PATH, paths.SECRET_KEY_PATH, index.DEFAULT_DB_PATH, status.STATUS_PATH)
    cache.CACHE_PATH = tmp / "translation_cache.sqlite3"
    settings.SETTINGS_PATH = tmp / "settings.json"
    paths.SECRET_KEY_PATH = tmp / "secret_key"
    index.DEFAULT_DB_PATH = tmp / "library.sqlite3"
    status.STATUS_PATH = tmp / "status.json"
    yield tmp
    cache.CACHE_PATH, settings.SETTINGS_PATH, paths.SECRET_KEY_PATH, index.DEFAULT_DB_PATH, status.STATUS_PATH = saved
    shutil.rmtree(tmp, ignore_errors=True)
    after = sorted(p.name for p in instance.iterdir()) if instance.is_dir() else None
    assert after == before, f"tests created files in the real instance/: {after} (before: {before})"
