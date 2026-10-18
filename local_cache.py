"""Small SQLite cache for translation and dictionary lookups."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "instance" / "translation_cache.sqlite3"


def _connect() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(CACHE_PATH, timeout=5)
    db.execute("CREATE TABLE IF NOT EXISTS cache (kind TEXT NOT NULL, cache_key TEXT NOT NULL, value TEXT NOT NULL, expires REAL, updated REAL NOT NULL, PRIMARY KEY(kind, cache_key))")
    db.execute("CREATE TABLE IF NOT EXISTS translation_usage (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, target TEXT NOT NULL, source_chars INTEGER NOT NULL, metered_chars INTEGER NOT NULL, created REAL NOT NULL)")
    return db


def record_translation_usage(provider: str, target: str, source_chars: int, metered_chars: int | None = None) -> None:
    """Record a successful uncached provider request; failures and cache hits are excluded."""
    if provider not in {"azure", "deepl"} or source_chars < 0:
        return
    now = time.time()
    try:
        with _connect() as db:
            db.execute(
                "INSERT INTO translation_usage(provider,target,source_chars,metered_chars,created) VALUES(?,?,?,?,?)",
                (provider, target, source_chars, max(0, metered_chars if metered_chars is not None else source_chars), now),
            )
    except (OSError, sqlite3.Error):
        # Usage reporting must never turn a successful translation into an error.
        return


def monthly_translation_usage() -> dict:
    """Return app-local totals for the current calendar month (server local time)."""
    now = datetime.now().astimezone()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        with _connect() as db:
            rows = db.execute(
                "SELECT provider, SUM(source_chars), SUM(metered_chars), COUNT(*) FROM translation_usage WHERE created >= ? GROUP BY provider",
                (start,),
            ).fetchall()
    except (OSError, sqlite3.Error):
        rows = []
    totals = {"azure": {"source_chars": 0, "metered_chars": 0, "requests": 0}, "deepl": {"source_chars": 0, "metered_chars": 0, "requests": 0}}
    for provider, source, metered, requests in rows:
        if provider in totals:
            totals[provider] = {"source_chars": int(source or 0), "metered_chars": int(metered or 0), "requests": int(requests or 0)}
    return {"month": f"{now.year}年{now.month}月", "providers": totals}


def get(kind: str, key: str):
    try:
        with _connect() as db:
            row = db.execute("SELECT value, expires FROM cache WHERE kind=? AND cache_key=?", (kind, key)).fetchone()
            if not row:
                return None
            if row[1] is not None and row[1] <= time.time():
                db.execute("DELETE FROM cache WHERE kind=? AND cache_key=?", (kind, key))
                return None
            return json.loads(row[0])
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None


def put(kind: str, key: str, value, ttl: int | None = None, max_entries: int = 500) -> None:
    try:
        now = time.time()
        expires = now + ttl if ttl else None
        with _connect() as db:
            db.execute("INSERT OR REPLACE INTO cache(kind, cache_key, value, expires, updated) VALUES(?,?,?,?,?)", (kind, key, json.dumps(value, ensure_ascii=False), expires, now))
            db.execute("DELETE FROM cache WHERE expires IS NOT NULL AND expires <= ?", (now,))
            db.execute("DELETE FROM cache WHERE rowid IN (SELECT rowid FROM cache WHERE kind=? ORDER BY updated DESC LIMIT -1 OFFSET ?)", (kind, max_entries))
    except (OSError, sqlite3.Error, TypeError, ValueError):
        # Caching is an optimization; provider results remain usable if SQLite is unavailable.
        return
